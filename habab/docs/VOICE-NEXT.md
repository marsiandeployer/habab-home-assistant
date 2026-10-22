# Голос и разговор с нейросетью

Схема: человек → микрофон → OpenAI / Яндекс / своё оборудование → команда устройству → ответ. Первой подключается действительно настроенная OpenAI-связка. Ненастроенные провайдеры не показываются как доступные.

Публичное демо управляет только виртуальными устройствами; настоящая коробка — отдельный локальный Docker Core. Голос включается по действию посетителя, текст работает без микрофона. Секреты на сервере, инструменты ограничены устройствами, без произвольных URL/shell. Проверка физического микрофона/iOS проводится отдельно от эмуляции.

Задачи: [#1 голос в демо](https://github.com/marsiandeployer/habab-home-assistant/issues/1), [#2 настоящий Assist](https://github.com/marsiandeployer/habab-home-assistant/issues/2), [#3 Яндекс](https://github.com/marsiandeployer/habab-home-assistant/issues/3), [#4 локальная речь](https://github.com/marsiandeployer/habab-home-assistant/issues/4).

Источники: [Assist](https://www.home-assistant.io/integrations/assist_pipeline/), [OpenAI Conversation](https://www.home-assistant.io/integrations/openai_conversation/), [Realtime](https://developers.openai.com/api/docs/guides/realtime-conversations).

Статус: не подключено. В Docker-пакет 0.1.0 готовый голосовой pipeline не входит.
