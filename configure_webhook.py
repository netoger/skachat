"""Проверка HTTPS и разовая регистрация webhook; ничего не запускает в фоне."""

import argparse
import asyncio
import logging
import os
import re
import secrets
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values, set_key

BASE_DIR = Path(__file__).resolve().parent


def check_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.port not in (None, 443) or parsed.path != '/webhook.py'):
        raise ValueError('Нужен адрес https://домен/webhook.py без секретов и параметров')
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False,
                      transport=httpx.HTTPTransport(local_address='0.0.0.0')) as client:
        if client.get(url).status_code != 405:
            raise RuntimeError('CGI ещё не отвечает ожидаемым HTTP 405')
        origin = f'https://{parsed.netloc}'
        for path in ['/.env', '/app/.env', '/webhook_cgi.py', '/data/webhook_state.pickle']:
            if client.get(origin + path).status_code not in (403, 404):
                raise RuntimeError('Не подтверждён запрет HTTP-доступа: ' + path)
        if client.post(url, content=b'{}', headers={'Content-Type': 'application/json'}).status_code not in (403, 503):
            raise RuntimeError('Webhook принимает запрос без секретного заголовка')


async def register(url, token, secret):
    import net_patch
    net_patch.install(watchdog=False)
    from telegram import Bot
    async with Bot(token) as bot:
        await bot.set_webhook(url, secret_token=secret, max_connections=1,
                              allowed_updates=['message', 'callback_query'], drop_pending_updates=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url')
    parser.add_argument('--apply', action='store_true', help='зарегистрировать после проверок')
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    check_url(args.url)
    print('HTTPS, CGI и запрет служебных файлов проверены.')
    if not args.apply:
        return
    # Старый экземпляр нельзя оставлять в цикле getUpdates после переключения.
    for filename in ['run_bot.py', 'bot.py']:
        result = subprocess.run(['pgrep', '-u', str(os.getuid()), '-f',
                                 re.escape(str(BASE_DIR / filename))], capture_output=True)
        if result.returncode != 1:
            raise RuntimeError('Сначала остановите прежний polling; webhook не изменён.')
    env_path = BASE_DIR / '.env'
    config = dotenv_values(env_path)
    if not config.get('BOT_TOKEN'):
        raise RuntimeError('В серверном .env не настроен BOT_TOKEN')
    secret = config.get('WEBHOOK_SECRET') or secrets.token_urlsafe(32)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', secret):
        raise ValueError('Некорректный WEBHOOK_SECRET в серверном .env')
    if not config.get('WEBHOOK_SECRET'):
        set_key(env_path, 'WEBHOOK_SECRET', secret)
        env_path.chmod(0o600)
    # Пустое служебное обновление проверяет backend без сообщения пользователю и ИИ.
    with httpx.Client(timeout=90, trust_env=False,
                      transport=httpx.HTTPTransport(local_address='0.0.0.0')) as client:
        response = client.post(args.url, json={'update_id': 0}, headers={
            'X-Telegram-Bot-Api-Secret-Token': secret,
        })
        if response.status_code != 200:
            raise RuntimeError('Backend не прошёл проверку; webhook не изменён.')
    asyncio.run(register(args.url, config['BOT_TOKEN'], secret))
    print('Webhook зарегистрирован. Фоновые службы не запущены.')


if __name__ == '__main__':
    main()
