# Habab Home Assistant

Русский Home Assistant для локального домашнего сервера: Docker-пакет и интерактивное демо. Это настоящий GitHub fork **home-assistant/frontend**, с общей историей и upstream для обновлений. Core пока используется из официального Docker-образа: это не fork полного Core.

**[Habab](https://habab.ru/) · [AI Home Server](https://habab.ru/products/ai_home_server) · [Русское демо](https://habab-demo.wpmix.net/) · [Поддержка](https://t.me/onoutnoxon)**

## Что работает

- Русский интерфейс демо с виртуальными светильниками, термостатом и сценами.
- Сохранение состояния отдельно для каждого посетителя через наш API и PostgreSQL.
- Docker-пакет Habab Home 0.1.0 для локального Linux-box. Облако по желанию; LAN или Wi-Fi нужны для связи с устройствами.
- Демонстрационная форма Telegram: токен, проверка формата и результат. Токен не передаётся и не сохраняется, настоящий бот не запускается.

Демо не управляет реальными устройствами. В Docker-дистрибутиве добавлен раздел AI с OpenAI, DeepSeek и Google Gemini и выполнением команд через Assist. Голосовая цепочка OpenAI/DeepSeek проверена на синтетическом аудио и виртуальной сущности; Gemini с настоящим ключом, физический микрофон, Яндекс, полностью локальная речь и Zigbee требуют отдельной проверки. [Настройка голоса](habab/docs/VOICE.md).

[Помощник OpenCode + DeepSeek](habab/docs/SETUP-ASSISTANT.md) выполняет команды внутри домашнего контейнера и настраивает интеграции через штатные API Home Assistant. В публичном демо работает отдельный чат с инструкциями. [Проверка на Windows](habab/distribution/README-WINDOWS.md): батник устанавливает недостающий Docker Desktop и использует Linux OpenCode в read-only volume. Ключи провайдеров в пакет не входят.

## Разработка и обновления

Наши дополнения находятся в **[habab/](habab/README.md)**, исходники frontend — в корневых demo/, src/ и build-scripts/. Скомпилированные чанки генерируются сборкой текущего checkout и не хранятся в Git.

```sh
git clone https://github.com/marsiandeployer/habab-home-assistant.git
cd habab-home-assistant
git remote add upstream https://github.com/home-assistant/frontend.git
```

[Установка и проверки](habab/README.md) · [Docker-пакет](habab/distribution/README.md) · [Как обновлять upstream](habab/docs/UPDATING.md)

## План

- [#1 Голос и AI-разговор через OpenAI](https://github.com/marsiandeployer/habab-home-assistant/issues/1)
- [#2 Assist в настоящей Docker-коробке](https://github.com/marsiandeployer/habab-home-assistant/issues/2)
- [#3 Яндекс](https://github.com/marsiandeployer/habab-home-assistant/issues/3) и [#4 локальная речь](https://github.com/marsiandeployer/habab-home-assistant/issues/4)
- [#5 Telegram-чат на основе aisell](https://github.com/marsiandeployer/habab-home-assistant/issues/5)
- [#6 Wi-Fi, Zigbee, железо и ARM64](https://github.com/marsiandeployer/habab-home-assistant/issues/6)
- [#7 Upstream-обновления и релизы](https://github.com/marsiandeployer/habab-home-assistant/issues/7)
- [#8 Облако по желанию](https://github.com/marsiandeployer/habab-home-assistant/issues/8)
- [#9 Инструкции и видео](https://github.com/marsiandeployer/habab-home-assistant/issues/9)

## Происхождение

Сохранена оригинальная лицензия [Apache-2.0](LICENSE.md). Изменения и атрибуция Habab — [NOTICE](habab/NOTICE.md). Это самостоятельный проект, не официальный сервис Home Assistant. Исходный baseline: 92599a88037ef5b7bd822efd7301028d824f7f5b.

<details>
<summary>Оригинальный README Home Assistant frontend</summary>

# Home Assistant Frontend

This is the repository for the official [Home Assistant](https://home-assistant.io) frontend.

[![Screenshot of the frontend](https://raw.githubusercontent.com/home-assistant/frontend/master/docs/screenshot.png)](https://demo.home-assistant.io/)

- [View demo of Home Assistant](https://demo.home-assistant.io/)
- [More information about Home Assistant](https://home-assistant.io)
- [Frontend development instructions](https://contributors.home-assistant.io/frontend/development/)

## Development

- Initial setup: `script/setup`
- Development: [Instructions](https://contributors.home-assistant.io/frontend/development/)
- Production build: `pnpm build`
- Gallery: `cd gallery && script/develop_gallery`

## Frontend development

### Classic environment

A complete guide can be found at the following [link](https://www.home-assistant.io/developers/frontend/). It describes a short guide for the build of project.

## License

Home Assistant is open-source and Apache 2 licensed. Feel free to browse the repository, learn and reuse parts in your own projects.

We use [BrowserStack](https://www.browserstack.com) to test Home Assistant on a large variety of devices.

[![Home Assistant - A project from the Open Home Foundation](https://www.openhomefoundation.org/badges/home-assistant.png)](https://www.openhomefoundation.org/)

</details>
