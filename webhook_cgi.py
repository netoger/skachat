"""Один HTTP-запрос — один запуск, без polling и фонового сервера."""

import asyncio
import hmac
import json
import logging
import os
import sys
from pathlib import Path

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


def handle_request(environ, stream):
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / '.env')
    payload = read_update(environ, stream, os.getenv('WEBHOOK_SECRET', ''))

    import fcntl
    data_dir = BASE_DIR / 'data'
    data_dir.mkdir(exist_ok=True)
    with (data_dir / 'webhook.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RequestError(503)
        # Импорт после блокировки: JSON-счётчик ИИ читается заново на запрос.
        import net_patch
        net_patch.install(watchdog=False)
        from bot import build_application
        from telegram.ext import PicklePersistence, PersistenceInput
        persistence = PicklePersistence(
            filepath=data_dir / 'webhook_state.pickle', on_flush=True,
            store_data=PersistenceInput(chat_data=False, callback_data=False),
        )
        asyncio.run(process_once(build_application(persistence), payload))


def main():
    os.umask(0o077)
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        handle_request(os.environ, sys.stdin.buffer)
        status = 200
    except RequestError as exc:
        status = exc.status
    except Exception:
        logging.exception('Ошибка обработки webhook')
        status = 503
    reasons = {200: 'OK', 400: 'Bad Request', 403: 'Forbidden', 405: 'Method Not Allowed',
               413: 'Content Too Large', 415: 'Unsupported Media Type', 503: 'Service Unavailable'}
    print(f'Status: {status} {reasons[status]}\r\nContent-Type: text/plain; charset=utf-8\r\n'
          f'Cache-Control: no-store\r\n\r\n{reasons[status]}')


if __name__ == '__main__':
    main()
