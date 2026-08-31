#!/usr/bin/env bash
# Управление службой бота на виртуальном хостинге, где нет ни Docker,
# ни systemd, ни доступа к crontab по SSH.
#
#   bash ~/skachat/skachatctl.sh start|stop|restart|status|logs|update|ensure
#
# `ensure` предназначен для планировщика в панели управления: он поднимает
# службу, если её нет, и молчит, если она уже работает.

set -u

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$APP_DIR/.venv/bin/python"
ENTRY="$APP_DIR/run_bot.py"
DATA_DIR="$APP_DIR/data"
LOG_FILE="$DATA_DIR/bot.log"
LOG_MAX_BYTES=$((10 * 1024 * 1024))
PATTERN="skachat/run_bot.py"

mkdir -p "$DATA_DIR"

running_pids() {
  pgrep -u "$(id -un)" -f "$PATTERN" 2>/dev/null
}

require_setup() {
  if [ ! -x "$VENV_PY" ]; then
    echo "Нет окружения: $VENV_PY. Создайте его заново (см. deploy/README-server.md)." >&2
    exit 1
  fi
  if [ ! -f "$APP_DIR/.env" ]; then
    echo "Нет $APP_DIR/.env — скопируйте .env.example и впишите токен." >&2
    exit 1
  fi
  if grep -qE '^BOT_TOKEN=(|PASTE_TOKEN_HERE|your_telegram_bot_token)$' "$APP_DIR/.env"; then
    echo "В $APP_DIR/.env не вписан настоящий BOT_TOKEN." >&2
    exit 1
  fi
  if [ ! -f "$APP_DIR/system_prompt.txt" ]; then
    echo "Нет $APP_DIR/system_prompt.txt — скопируйте system_prompt.example.txt." >&2
    exit 1
  fi
}

ai_status() {
  # Импорт bot.py не требует токена, поэтому проверка безопасна и без запуска
  "$VENV_PY" - <<'PY' 2>/dev/null || echo "не удалось определить"
import bot
try:
    c = bot.resolve_ai_config()
    print(f"настроен — {c.provider}, модель {c.model}")
except Exception as exc:
    print(f"НЕ НАСТРОЕН — {exc}")
PY
}

rotate_log() {
  # Простая ротация: журнал на шаред-хостинге незаметно съедает квоту
  if [ -f "$LOG_FILE" ]; then
    local size
    size=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
    if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
      mv -f "$LOG_FILE" "$LOG_FILE.1"
    fi
  fi
}

do_stop() {
  local pids
  pids=$(running_pids)
  if [ -z "$pids" ]; then
    echo "Служба не запущена."
    return 0
  fi
  echo "Останавливаю: $pids"
  kill $pids 2>/dev/null
  for _ in $(seq 1 15); do
    [ -z "$(running_pids)" ] && break
    sleep 1
  done
  pids=$(running_pids)
  if [ -n "$pids" ]; then
    kill -9 $pids 2>/dev/null
    sleep 1
  fi
  echo "Остановлена."
}

do_start() {
  require_setup
  if [ -n "$(running_pids)" ]; then
    echo "Служба уже работает (PID: $(running_pids | tr '\n' ' '))."
    return 0
  fi
  rotate_log
  cd "$APP_DIR" || exit 1
  # setsid + nohup: процесс переживает выход из SSH
  setsid nohup "$VENV_PY" "$ENTRY" >> "$LOG_FILE" 2>&1 < /dev/null &
  sleep 4
  do_status
}

do_status() {
  local pids
  pids=$(running_pids)
  if [ -z "$pids" ]; then
    echo "СТАТУС: не работает"
    echo "ИИ-чат: $(ai_status)"
    echo "--- последние строки журнала ---"
    tail -n 15 "$LOG_FILE" 2>/dev/null || echo "(журнала нет)"
    return 1
  fi
  echo "СТАТУС: работает"
  ps -o pid,etime,rss,cmd -p $(echo "$pids" | tr '\n' ',' | sed 's/,$//') 2>/dev/null | tail -n +1
  echo "Адрес Telegram: $(cat "$DATA_DIR/tg_endpoint.txt" 2>/dev/null || echo 'ещё не выбран')"
  echo "ИИ-чат: $(ai_status)"
  echo "--- последние строки журнала ---"
  tail -n 10 "$LOG_FILE" 2>/dev/null
}

case "${1:-status}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  do_status ;;
  logs)    tail -n "${2:-50}" -f "$LOG_FILE" ;;
  ensure)
    # Для планировщика: тихо поднимаем службу, если она упала
    if [ -z "$(running_pids)" ]; then
      echo "$(date '+%F %T') служба не найдена, запускаю" >> "$DATA_DIR/watchdog.log"
      do_start >> "$DATA_DIR/watchdog.log" 2>&1
    fi
    ;;
  update)
    cd "$APP_DIR" || exit 1
    if ! git pull --ff-only; then
      echo "" >&2
      echo "Обновление не применено. Если git жалуется на локальные изменения," >&2
      echo "верните файлы к индексу и повторите:" >&2
      echo "  cd $APP_DIR && git checkout -- . && bash skachatctl.sh update" >&2
      exit 1
    fi
    if ! "$VENV_PY" -m pip install --quiet --upgrade -r requirements.txt; then
      echo "Зависимости не установились — служба не перезапущена, старая версия работает." >&2
      exit 1
    fi
    do_stop
    if ! do_start; then
      echo "" >&2
      echo "Служба не поднялась после обновления. Журнал: $LOG_FILE" >&2
      exit 1
    fi
    ;;
  *)
    echo "Использование: $0 {start|stop|restart|status|logs|update|ensure}" >&2
    exit 2
    ;;
esac
