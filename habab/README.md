# Компоненты Habab Home

[Наш сайт](https://habab.ru/) · [AI Home Server](https://habab.ru/products/ai_home_server) · [Демо](https://habab-demo.wpmix.net/) · [Поддержка](https://t.me/onoutnoxon)

- public/ — опубликованная сборка и независимые формы в public/integrations/.
- app.py, habab_demo/ — Flask API состояния виртуальных устройств с PostgreSQL и изоляцией сессий.
- distribution/ — исходный кандидат Habab Home 0.3.6 на Home Assistant Core 2026.10.2, без OS/Supervisor, с frontend `20260930.4+habab.9`. Пакет основного Windows-загрузчика — 0.3.1; отдельный [preview-загрузчик кандидата 0.3.6](https://habab-demo.wpmix.net/downloads/start-home-assistant-preview-0.3.6-45203ea388bf.bat) предназначен для проверки актуального кандидата.
- build/ — русский перевод, publisher и архив исходного patch.
- deploy/, ecosystem.config.cjs — пример deployment нашего сервера, адаптируйте пути и роли PostgreSQL.
- qa/, tests/ — браузерные проверки и тесты API.

Форма Telegram демонстрационная: токен не передаётся и не сохраняется, бот не запускается. Демо не управляет реальным домом. В Docker-дистрибутиве есть отдельный административный раздел AI: провайдеры, проверка API, команды и голос через Assist. [Инструкция и проверенный объём](docs/VOICE.md).

[Чат помощника](docs/SETUP-ASSISTANT.md) — один административный чат, который автоматически открывается после авторизованной загрузки Home Assistant на любой странице. На компьютере это немодальная панель справа. На телефоне сверху остаётся дашборд, снизу открывается чат; его можно свернуть или раскрыть на весь экран. Кнопка на странице **AI** ведёт в ту же панель, отдельного дубля «Дом сейчас» нет.

Веб и Telegram используют общий `AssistantService` и приватный OpenCode runtime с отдельными историями. Нативный Assist вызывает тот же сервис через адаптер с доступом только к инструментам Home Assistant, разрешённым для этой конфигурации. Модель получает только имена, типы и обязательность полей; варианты, подписи, значения, defaults и URL ей не передаются. В разделе Telegram администратор подключает бота, привязывает личный чат владельца и при необходимости открывает доступ доверенной группе. Участники группы пишут боту в личные сообщения; сообщения в самой группе он игнорирует. Обычные и preview-страницы публичного демо используют общий чат и prompt. Демо отвечает через DeepSeek без инструментов и доступа к устройствам. [Запуск на Windows](distribution/README-WINDOWS.md).

После скачивания репозитория на Windows откройте `habab\distribution\start-windows.bat` двойным кликом. При первом запуске Home Assistant создайте собственный аккаунт: готовых логина и пароля в пакете нет.

Чтобы подключить Xiaomi Home, откройте чат внутри Home Assistant и напишите
«Подключи Xiaomi Home в России». Помощник откроет карточку установки и входа;
отдельный Xiaomi BAT для этого сценария не нужен.
Временный доступ поддержки запускает `habab\distribution\support_tunnel.bat`.
[Как подключается поддержка и куда отправляется телеметрия](support/README.md).

## API и проверки

Из корня репозитория:

```sh
python3 -m venv .venv
.venv/bin/pip install -r habab/requirements-dev.txt
cp habab/.env.example .env
```

В приватном .env задайте уникальный SECRET_KEY и HA_DEMO_DATABASE_DSN своей PostgreSQL. Создайте схему из habab/deploy/state.sql в нужной базе. Не публикуйте .env или пользовательские данные.

```sh
cd habab
../.venv/bin/gunicorn --bind 127.0.0.1:8318 app:application
# В отдельном терминале из habab/:
timeout 60s ../.venv/bin/python3 -m pytest -q
npm ci --ignore-scripts
timeout 120s npm run test:browser
```

На нашем сервере API управляется PM2 (habab-home-demo), nginx обслуживает habab/public/. Ключ сессии и база при переносе сохранены; не меняйте их без подготовки миграции.

Браузерные проверки используют /snap/bin/chromium и публичное демо. Не запускайте их против чужого production без разрешения. CDP-проверки требуют отдельного принадлежащего проверке endpoint.

Сборка текущих исходников: sh habab/build/build.sh из корня. [Обновление upstream](docs/UPDATING.md) · [Docker-установка](distribution/README.md).
