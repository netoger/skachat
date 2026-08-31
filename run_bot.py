"""Точка входа на сервере: сначала чиним сеть, потом запускаем бота.

Отдельный файл нужен, чтобы `bot.py` остался ровно таким, как в репозитории,
и `git pull` не приводил к конфликтам.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Формат совпадает с тем, что настраивает bot.py, поэтому его собственный
# вызов basicConfig ничего не изменит, а сообщения о подборе адреса Telegram
# уже будут оформлены.
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)

import net_patch  # noqa: E402  — должен отработать до любых сетевых вызовов

net_patch.install()

from bot import main  # noqa: E402  — импорт после починки сети

if __name__ == "__main__":
    main()
