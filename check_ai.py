"""Проверка провайдера ИИ прямо с сервера.

Нужна потому, что доступность провайдера с российского хостинга — не
данность: Groq, например, отвечает с vh470 «403 Forbidden» без единого
слова про ключ. Скрипт показывает, что именно выбрано в `.env`, доходит
ли запрос и что отвечает провайдер, не запуская самого бота.

    cd ~/skachat && .venv/bin/python check_ai.py

Ключи не печатаются: в выводе только длина и последние символы.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(format="%(levelname)s | %(message)s", level=logging.WARNING)

import net_patch  # noqa: E402  — до любых сетевых вызовов

net_patch.install()

import bot  # noqa: E402  — сам подхватывает .env


def mask(secret: str) -> str:
    if not secret:
        return "(пусто)"
    return f"…{secret[-4:]} ({len(secret)} символов)"


async def main() -> int:
    try:
        config = bot.resolve_ai_config()
    except RuntimeError as exc:
        print(f"Провайдер не настроен: {exc}")
        return 2

    print(f"AI_PROVIDER в .env: {bot.AI_PROVIDER}")
    print(f"выбран провайдер:   {config.provider}")
    print(f"адрес:              {config.api_url}")
    print(f"модель:             {config.model}")
    print(f"ключ:               {mask(config.api_key)}")
    print("-" * 60)

    try:
        answer = await bot.ask_ai("Ответь одним словом: работает?", [])
    except Exception as exc:  # noqa: BLE001 — тут интересна любая причина
        detail = getattr(getattr(exc, "response", None), "text", "")
        print(f"ОШИБКА {type(exc).__name__}: {exc}")
        if detail:
            print(f"ответ провайдера: {detail[:500]}")
        print(
            "\nЕсли это 403 без упоминания ключа — скорее всего гео-блокировка "
            "хостинга, и провайдера надо менять, а не ключ."
        )
        return 1

    print(f"ответ модели: {answer[:300]}")
    print("\nПровайдер работает.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
