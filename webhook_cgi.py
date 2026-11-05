"""Один HTTP-запрос — один запуск, без polling и фонового сервера."""

import asyncio
import hmac
import json
import logging
import os
import sys
import time
from pathlib import Path
from contextlib import contextmanager, suppress

BASE_DIR = Path(__file__).resolve().parent


class RequestError(Exception):
    def __init__(self, status):
        self.status = status


def read_update(environ, stream, secret):
    if environ.get('REQUEST_METHOD') != 'POST':
        raise RequestError(405)
    if not secret:
        raise RequestError(503)
    received = environ.get('HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN', '')
    if not hmac.compare_digest(received.encode(), secret.encode()):
        raise RequestError(403)
    if environ.get('CONTENT_TYPE', '').split(';')[0].strip() != 'application/json':
        raise RequestError(415)
    try:
        size = int(environ.get('CONTENT_LENGTH', '0'))
    except ValueError:
        raise RequestError(400)
    if size > 1024 * 1024:
        raise RequestError(413)
    if size <= 0:
        raise RequestError(400)
    try:
        body = stream.read(size)
        if len(body) != size:
            raise ValueError('Неполный запрос')
        payload = json.loads(body)
        update_id = payload.get('update_id')
        if type(update_id) is not int or update_id < 0:
            raise ValueError('Некорректный update_id')
        return payload
    except (ValueError, AttributeError, UnicodeError):
        raise RequestError(400)


async def process_once(application, payload):
    from telegram import Update

    async with application:
        completed = application.bot_data.setdefault('webhook_completed', [])
        if payload['update_id'] in completed:
            return
        errors = []

        async def remember_error(update, context):
            errors.append(context.error)

        application.add_error_handler(remember_error)
        await application.process_update(Update.de_json(payload, application.bot))
        if errors:
            raise RuntimeError('Обработчик webhook завершился с ошибкой') from errors[0]
        completed.append(payload['update_id'])
        # ponytail: последние 2000 обновлений; для больших очередей нужна БД.
        del completed[:-2000]
        await application.update_persistence()
        await application.persistence.flush()


def request_user_id(payload):
    body = payload.get('message') or payload.get('callback_query') or {}
    value = body.get('from', {}).get('id')
    return value if type(value) is int and value > 0 else 0


async def prepare_persistence(data_dir, user_id):
    from telegram.ext import PicklePersistence, PersistenceInput
    users_dir = data_dir / 'users'
    users_dir.mkdir(exist_ok=True)
    path = users_dir / f'{user_id}.pickle'
    persistence = PicklePersistence(filepath=path, on_flush=True,
                                   store_data=PersistenceInput(chat_data=False, callback_data=False))
    legacy_path = data_dir / 'webhook_state.pickle'
    if not path.exists() and legacy_path.exists():
        legacy = PicklePersistence(filepath=legacy_path)
        old_user = (await legacy.get_user_data()).get(user_id, {})
        await persistence.update_user_data(user_id, {'chat_history': old_user.get('chat_history', [])})
        await persistence.update_bot_data({
            'webhook_completed': (await legacy.get_bot_data()).get('webhook_completed', []),
        })
        await persistence.flush()
    return persistence


def prepare_usage_file(data_dir, user_id, legacy_path):
    users_dir = data_dir / 'users'
    users_dir.mkdir(exist_ok=True)
    path = users_dir / f'{user_id}.usage.json'
    if not path.exists() and legacy_path.exists():
        data = json.loads(legacy_path.read_text(encoding='utf-8'))
        data['counts'] = {str(user_id): data.get('counts', {}).get(str(user_id), 0)}
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data), encoding='utf-8')
        temporary.replace(path)
    return path


@contextmanager
def request_locks(data_dir, user_id):
    import fcntl
    data_dir.mkdir(exist_ok=True)
    with (data_dir / 'webhook.lock').open('a') as lock:
        try:
            # Общая разделяемая блокировка пропускает новые запросы параллельно,
            # но ждёт завершения прежнего обработчика с эксклюзивной блокировкой.
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RequestError(503)
        users_dir = data_dir / 'users'
        users_dir.mkdir(exist_ok=True)
        with (users_dir / f'{user_id}.lock').open('a') as user_lock:
            try:
                fcntl.flock(user_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RequestError(503)
            yield


def handle_request(environ, stream):
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / '.env')
    payload = read_update(environ, stream, os.getenv('WEBHOOK_SECRET', ''))
    data_dir = BASE_DIR / 'data'
    user_id = request_user_id(payload)
    with request_locks(data_dir, user_id):
        legacy_usage = Path(os.getenv('AI_USAGE_FILE', str(data_dir / 'ai_usage.json')))
        os.environ['AI_USAGE_FILE'] = str(prepare_usage_file(data_dir, user_id, legacy_usage))
        # Импорт после блокировки и выбора личного счётчика ИИ.
        import net_patch
        net_patch.install(watchdog=False)
        from bot import build_application

        async def process_user():
            persistence = await prepare_persistence(data_dir, user_id)
            await process_once(build_application(persistence), payload)

        asyncio.run(process_user())


def main():
    os.umask(0o077)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    started = time.monotonic()
    try:
        handle_request(os.environ, sys.stdin.buffer)
        status = 200
    except RequestError as exc:
        status = exc.status
    except Exception as exc:
        # Для диагностики достаточно типов ошибок и длительности:
        # тексты сообщений, идентификаторы и секретные URL сюда не записываем.
        types = []
        cause = exc
        while cause is not None and len(types) < 5:
            types.append(type(cause).__name__)
            cause = cause.__cause__
        with suppress(OSError):
            with (BASE_DIR / 'data' / 'webhook_errors.log').open('a') as output:
                output.write(json.dumps({'error_types': types,
                                         'seconds': round(time.monotonic() - started, 2)}) + '\n')
        logging.exception('Ошибка обработки webhook')
        status = 503
    reasons = {200: 'OK', 400: 'Bad Request', 403: 'Forbidden', 405: 'Method Not Allowed',
               413: 'Content Too Large', 415: 'Unsupported Media Type', 503: 'Service Unavailable'}
    print(f'Status: {status} {reasons[status]}\r\nContent-Type: text/plain; charset=utf-8\r\n'
          f'Cache-Control: no-store\r\n\r\n{reasons[status]}')


if __name__ == '__main__':
    main()
