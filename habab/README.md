# Компоненты Habab Home

[Наш сайт](https://habab.ru/) · [AI Home Server](https://habab.ru/products/ai_home_server) · [Демо](https://habab-demo.wpmix.net/) · [Поддержка](https://t.me/onoutnoxon)

- public/ — опубликованная сборка и независимые формы в public/integrations/.
- app.py, habab_demo/ — Flask API состояния виртуальных устройств с PostgreSQL и изоляцией сессий.
- distribution/ — Docker-пакет Habab Home 0.3.0 на Home Assistant Core 2026.10.2, без OS/Supervisor.
- build/ — русский перевод, publisher и архив исходного patch.
- deploy/, ecosystem.config.cjs — пример deployment нашего сервера, адаптируйте пути и роли PostgreSQL.
- qa/, tests/ — браузерные проверки и тесты API.

Форма Telegram демонстрационная: токен не передаётся и не сохраняется, бот не запускается. Демо не управляет реальным домом. В Docker-дистрибутиве есть отдельный административный раздел AI: провайдеры, проверка API, команды и голос через Assist. [Инструкция и проверенный объём](docs/VOICE.md).

[Чат помощника](docs/SETUP-ASSISTANT.md) открывается одной глобальной кнопкой в правом нижнем углу: на компьютере появляется немодальная панель справа, на телефоне — полноэкранный чат. Страница **AI** открывает тот же чат. Веб и Telegram используют общий `AssistantService` и приватный OpenCode runtime с отдельными историями. Нативный Assist вызывает тот же сервис через адаптер с доступом только к инструментам Home Assistant, разрешённым для этой конфигурации. Модель видит только имена, типы и обязательность полей. Варианты, подписи, defaults, введённые значения и URL ей не передаются. В боковом меню есть административный раздел Telegram для токена BotFather и привязки личного чата. Публичное демо отвечает через DeepSeek без инструментов и доступа к устройствам. [Запуск на Windows](distribution/README-WINDOWS.md).

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
