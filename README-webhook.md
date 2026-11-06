# Timeweb: webhook без постоянного процесса

Состояние на 06.11.2026: выполнена выкладка и зарегистрирован webhook
https://togeer.tw1.ru/webhook.py. Webroot: /home/c/cc250576/togeeer/public_html.
Исходный git-клон: /home/c/cc250576/skachat, ветка codex/timeweb-webhook.
Рабочий backend: /home/c/cc250576/togeeer/public_html/app, detached git-worktree
того же репозитория. 27 автономных тестов проходят на Linux-сервере (на Windows два Linux-теста
пропускаются), CGI и TLS проверены,
секреты и данные закрыты HTTP 403. getWebhookInfo: pending=0;
поле last_error может сохранять историческую ошибку после восстановления.
Прежний polling остановлен, BOT_MODE=webhook установлен в серверном .env.
Реальная отправка ролика пользователем через новый webhook ещё не проверена.
Остальные сайты не изменять. start/restart/update в skachatctl.sh заблокированы,
ensure ничего не запускает. Для обновления backend после сверки git status
обновлять и исходный клон, и рабочий worktree по инструкции ниже.

## Что нужно в панели

1. Создать отдельный сайт для бота, не выбирать корень другого проекта.
2. В «Домены и SSL» → «Мои домены» добавить свободный технический домен,
   например `skachatbot.tw1.ru`, и привязать к новому сайту. Это пример,
   доступность имени не проверена. По документации Timeweb SSL уже включён.
3. Передать агенту адрес и созданный панелью webroot.

## Размещение

CGI этой площадки не может читать backend вне webroot: stat/access могут
проходить, а open/listdir возвращают PermissionError. Это проверено именно
для текущего сайта, не обобщать на все Python-площадки.

В webroot лежат `.htaccess` и `webhook.py`, backend и зависимости — в `app/`.
Launcher запускается `/usr/bin/python3` (3.10.12), добавляет библиотеки
`app/.venv/lib/python3.10/site-packages` **перед** системными пакетами, затем
импортирует backend. Venv-интерпретатор в shebang здесь даёт HTTP 500;
добавление библиотек в конец sys.path вызывает конфликт cffi 2.1.1/1.15.0.

`app/.htaccess` содержит Require all denied, корневой .htaccess запрещает /app/.
Но nginx отдаёт некоторые статические расширения в обход Apache: проверка
обнаружила HTTP 200 для тестовых .txt/.js даже при app=700. Поэтому приватный
prompt хранится как `app/system_prompt.php`, секреты — `app/.env`, состояние —
в `app/data` (.json/.pickle/.log). Для этих реальных путей HTTP 403 проверен.
Видео хранятся только в `/tmp/skachat-cc250576-downloads`, вне HTTP-каталогов.
Не класть приватные .txt, медиа, архивы или резервные копии в app.

Истории, лимиты, prompt и .env скопированы из старого backend; оригиналы оставлены
в ~/skachat как резервная копия. Боевые данные теперь только в app. Старый .env
не использовать для изменения настроек работающего CGI. Личные cookies не копировались.
app и data — 700, .env/prompt — 600; публичный webhook.py — 755, .htaccess — 644.
Файлы Linux должны иметь LF.

Используется штатный CGI Apache, не `run_webhook`, ASGI или отдельный HTTP-сервер.
Нужны исходящие HTTPS-запросы Telegram, TikWM и CDN видео. Персональные
cookies не нужны TikWM. Instagram получает оглавление видео через VXInstagram,
используя профиль TelegramBot для страницы и MP4; прямой embed Instagram —
анонимный резерв. При обычном профиле файла VXInstagram отвечает 302.

После исправления проверены **полные файлы через CGI**, не только SSH:
TikTok — 2 823 850 байт, Shorts — 13 370 661 байт, Reels — 8 325 643 байта
дважды за 1,54 и 1,33 с. Служебные тесты не отправляли сообщения и не вызывали ИИ.

## Обновление кода

Сначала сверить git status обоих каталогов; при чужих изменениях не делать reset/checkout.
Данные, .env, prompt и .venv сохраняются; не копировать старые поверх боевых.

```bash
cd /home/c/cc250576/skachat
git diff --quiet && git diff --cached --quiet
git pull --ff-only origin codex/timeweb-webhook
git -C /home/c/cc250576/togeeer/public_html/app diff --quiet
git -C /home/c/cc250576/togeeer/public_html/app merge --ff-only "$(git rev-parse HEAD)"
```

Если изменился launcher — скопировать timeweb-public/webhook.py в webroot,
сохранив 755. Корневой .htaccess брать из timeweb-public/.htaccess,
защиту app — из timeweb-public/app.htaccess. При обновлении зависимостей
запускать `app/.venv/bin/python -m pip`, а не скопированный скрипт pip.

## Проверка и переключение

После выкладки выполнить на сервере (DOMAIN заменить реальным адресом):

```bash
cd /home/c/cc250576/togeeer/public_html/app
.venv/bin/python configure_webhook.py https://DOMAIN/webhook.py
```

Проверка не меняет webhook. GET обработчика должен возвращать 405,
служебные пути — 403/404, POST без секрета — 403 либо 503, если секрет
ещё не создан. TLS-сертификат проверяется штатно.

Только после успешных проверок остановить **именно прежний экземпляр skachat**
и отключить его start/ensure-задачу в панели, если она существует. Старый
polling не перезапускать. Затем:

```bash
.venv/bin/python configure_webhook.py https://DOMAIN/webhook.py --apply
```

Команда откажется переключать webhook при обнаружении старого polling,
создаст WEBHOOK_SECRET в серверном `.env`, не выводя его, и зарегистрирует
HTTPS-адрес после подписанного служебного запроса к backend. Он проверяет
связь с Telegram без сообщений пользователю и запросов ИИ.
Накопленные обновления Telegram не удаляются. `max_connections=3` позволяет
обрабатывать разных пользователей параллельно. У каждого своя блокировка и
файлы `data/users/USER_ID.pickle`, `USER_ID.usage.json`; один пользователь
обрабатывается последовательно. Общие файловые слоты ограничивают скачивания
тремя на аккаунт. Истории и квоты переносятся при первом запросе из старых
webhook_state.pickle и ai_usage.json — эти файлы пока не удалять.
Pickle-файлы доступны только backend, не принимать их из непроверенных источников.

После переключения проверить /start, повтор одного сообщения, два сообщения
диалога, TikTok и отсутствие getUpdates-процесса. Платный ИИ проверять только
по согласованию. CGI запускается и завершается на HTTP-запрос, сторожевой
поток net_patch отключён; адрес Telegram проверяется на каждом запуске.

Долгая обработка может вызвать повторную доставку Telegram. Последние 2000
завершённых update_id для каждого пользователя сохраняются и повторно не обрабатываются. При аварии
между отправкой ответа и сохранением состояния повтор ответа всё ещё возможен.
Для большой нагрузки потребуется очередь и другой тариф/площадка.
При проверке ссылки и скачивании действует анимация одного сообщения с интервалом
около двух секунд, перед отправкой — статус «Отправляю видео…».
Подписка на оба канала обязательна для всех; положительный кеш отключён,
при ошибке Telegram доступ не открывается. Бот имеет права администратора в обоих.
Ошибки CGI диагностируются в data/webhook_errors.log: только типы исключений
и длительность, без текстов сообщений и секретных URL.

## Автономная проверка

```powershell
.\.venv\Scripts\python.exe -m unittest test_external_download test_webhook_cgi test_bot_behavior
```

Тесты используют подменённые HTTP-ответы, без ключей, Telegram и платного ИИ.

Источники: [технические домены](https://timeweb.com/ru/docs/domeny/testovyj-tekhnicheskij-domen/),
[CGI/WSGI Python](https://timeweb.com/ru/docs/virtualnyj-hosting/prilozheniya-i-frejmvorki/django/),
[ограничения служб](https://timeweb.com/ru/docs/virtualnyj-hosting/obshchaya-informaciya-o-hostinge/pravila-i-ogranicheniya-na-hostinge/).
