"""Обход фильтрации сети до Telegram на российском хостинге.

С этой площадки `api.telegram.org` отвечает лишь с части своих адресов,
причём DNS стабильно отдаёт как раз отфильтрованный: соединение не
отвергается, а висит до таймаута, и бот выглядит «просто медленным».

Модуль подбирает живой адрес, запоминает его в файле и подменяет
системное разрешение имён, чтобы httpx (а с ним python-telegram-bot)
ходил именно туда. Имя хоста при этом сохраняется, поэтому SNI и
проверка сертификата работают как обычно — это не отключение TLS.

Заодно из выдачи вычёркивается IPv6: с этой площадки он не отвечает
вовсе, а DNS часто ставит его первым.
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TELEGRAM_HOST = "api.telegram.org"

# Запасные адреса api.telegram.org. Нужны потому, что DNS здесь бесполезен:
# он отдаёт отфильтрованный адрес, и без списка перебирать было бы нечего.
FALLBACK_IPS = (
    "149.154.167.220",
    "149.154.167.197",
    "149.154.167.198",
    "149.154.167.199",
    "149.154.166.110",
    "149.154.175.50",
    "149.154.171.5",
    "91.108.56.130",
)

CONNECT_TIMEOUT = 3.0     # заблокированный адрес молча висит — ждать его дольше нечего
RECHECK_INTERVAL = 120.0  # раз в 2 минуты проверяем, жив ли выбранный адрес

STATE_FILE = Path(
    os.getenv(
        "TG_ENDPOINT_FILE",
        str(Path(__file__).resolve().parent / "data" / "tg_endpoint.txt"),
    )
)

_original_getaddrinfo = socket.getaddrinfo
_lock = threading.Lock()
_current_ip: str | None = None


def _tls_ok(ip: str) -> bool:
    """Полное рукопожатие: часть адресов принимает TCP, но обрывает TLS."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((ip, 443), timeout=CONNECT_TIMEOUT) as raw:
            with context.wrap_socket(raw, server_hostname=TELEGRAM_HOST):
                return True
    except (OSError, ssl.SSLError):
        return False


def _dns_ips() -> list[str]:
    try:
        infos = _original_getaddrinfo(TELEGRAM_HOST, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def _saved_ip() -> list[str]:
    try:
        return [STATE_FILE.read_text(encoding="utf-8").strip()]
    except OSError:
        return []


def _save_ip(ip: str) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(ip + "\n", encoding="utf-8")
    except OSError:
        logger.warning("Не удалось сохранить рабочий адрес Telegram в %s", STATE_FILE)


def _candidates() -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ip in (*_saved_ip(), *_dns_ips(), *FALLBACK_IPS):
        if ip and ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


def _pick_ip(attempts: int = 2) -> str | None:
    """Перебор: сначала запомненный адрес, потом DNS, потом запасные.

    Список проходится несколько раз: фильтр изредка обрывает и живое
    соединение, поэтому одной неудачи мало, чтобы счесть адрес мёртвым.
    """
    candidates = _candidates()
    for attempt in range(attempts):
        for ip in candidates:
            if _tls_ok(ip):
                return ip
        if attempt + 1 < attempts:
            time.sleep(1.0)
    return None


def _is_telegram(host: object) -> bool:
    if isinstance(host, (bytes, bytearray)):
        name = host.decode("ascii", "ignore")
    elif isinstance(host, str):
        name = host
    else:
        return False
    return name.lower().rstrip(".") == TELEGRAM_HOST


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
    results = _original_getaddrinfo(host, port, family, type, proto, flags)
    ipv4 = [item for item in results if item[0] == socket.AF_INET]

    with _lock:
        pinned = _current_ip

    if pinned and _is_telegram(host):
        # Подменяем только адрес, оставляя тип сокета и порт как просили
        if ipv4:
            return [
                (fam, typ, prot, canon, (pinned, addr[1]))
                for fam, typ, prot, canon, addr in ipv4
            ]
        return [(socket.AF_INET, type or socket.SOCK_STREAM, proto, "", (pinned, port))]

    return ipv4 or results


def _watchdog() -> None:
    """Самовосстановление: если запомненный адрес замолчал, ищем следующий."""
    global _current_ip
    while True:
        time.sleep(RECHECK_INTERVAL)
        with _lock:
            pinned = _current_ip
        if pinned and _tls_ok(pinned):
            continue

        fresh = _pick_ip()
        if fresh and fresh != pinned:
            with _lock:
                _current_ip = fresh
            _save_ip(fresh)
            logger.warning("Адрес Telegram %s перестал отвечать, перешли на %s", pinned, fresh)
        elif not fresh:
            logger.error("Ни один известный адрес Telegram не отвечает")


def install() -> str | None:
    """Включить подмену. Вызывать до первого сетевого запроса."""
    global _current_ip

    ip = _pick_ip()
    if ip:
        _save_ip(ip)
        logger.info("Telegram доступен через %s", ip)
    else:
        # Оставлять разрешение имён нетронутым нельзя: DNS отдаёт как раз
        # отфильтрованный адрес, и бот гарантированно завис бы на нём.
        # Берём лучшее предположение, а сторож ниже подберёт живой адрес.
        fallback = _candidates()
        ip = fallback[0] if fallback else None
        logger.error(
            "Ни один известный адрес %s сейчас не отвечает; пробуем %s, "
            "сторож переключится, как только связь появится",
            TELEGRAM_HOST,
            ip or "нечего",
        )

    with _lock:
        _current_ip = ip

    socket.getaddrinfo = _patched_getaddrinfo
    threading.Thread(target=_watchdog, name="tg-endpoint-watchdog", daemon=True).start()
    return ip


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s | %(message)s", level=logging.INFO)
    print(install() or "адрес не найден")
