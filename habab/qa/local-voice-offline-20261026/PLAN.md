# План offline-профиля: русский Assist через Wyoming и Ollama

Статус: план и операторский source-каркас; образы, модели и контейнеры не запускались. Дата проверки метаданных: 26 октября 2026 года.

Цель — проверить на изолированной копии Home Assistant Core путь «синтетическая русская речь → Wyoming Whisper → локальный Ollama agent → одна виртуальная лампа → Wyoming Piper». Вызов должен пройти через настроенный conversation agent Ollama. Native Assist intents сами по себе не подтверждают работу LLM.

Проверка не затрагивает установленный Habab, владельческий Core, домашние устройства, микрофон, динамик или сеть владельца. Этот план не подтверждает скорость на целевом оборудовании и не закрывает issue #4.

## Подтверждённая основа

- Текущая закреплённая сборка Habab использует Home Assistant Core `2026.10.2`, образ `ghcr.io/home-assistant/home-assistant:2026.10.2`, digest `sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4`, лицензия Apache-2.0. Сверено с `habab/distribution/release.json`.
- Home Assistant описывает Wyoming STT/TTS и Ollama как отдельные локальные компоненты. Интеграция Ollama требует внешнего Ollama server; `Control Home Assistant` экспериментален, ограничивается exposed entities и требует model с tool support. Для этого профиля будет открыт только `light.qa_virtual_light`.
- HA называет русский среди языков, поддерживаемых Whisper; точность конкретной записи всё равно измеряется отдельно. Piper публикует русские голоса; для кандидата `ru_RU-denis-medium` указаны модель 63,201,294 байта и конфиг 4,823 байта. Карточка говорит о CC0 для датасета Denis и о fine-tuning от Lessac; права на сам голос нужно сверить перед любым распространением.
- Target CPU/RAM/GPU пока не зафиксированы. Поэтому ниже приведены размеры кандидатов и бюджет загрузки, но нет обещания скорости или минимальных аппаратных требований.

## Образы-кандидаты и зафиксированные registry digests

Digests получены чтением Docker Registry v2 manifests без скачивания image layers. Указаны multi-architecture manifest-list digest и размеры сжатых layers для обеих платформ. Перед будущим pull digest нужно повторно проверить.

| Компонент | Кандидат | Manifest-list digest | Linux amd64 / arm64 image layers | Лицензия и оговорка |
|---|---|---|---:|---|
| Home Assistant Core | `ghcr.io/home-assistant/home-assistant:2026.10.2` | `sha256:33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4` | 652,710,453 / 637,862,647 bytes | Apache-2.0; digest также закреплён в `habab/distribution/release.json`. |
| Whisper Wyoming | `rhasspy/wyoming-whisper:3.8.1` | `sha256:ba6fcb6056ebe237d15a325381763a80af2fc8fcedaa04f6a714a7375eb20d80` | 477,255,465 / 368,977,553 bytes | Wyoming Faster Whisper source repo — MIT. Проверять содержимое выбранного image tag и зависимостей при выпуске профиля. |
| Piper Wyoming | `rhasspy/wyoming-piper:2.5.2` | `sha256:7d39aafac409c2b6b09d999ed04d84c311d632e558b35baa60fad6aac722af7a` | 197,024,839 / 194,747,990 bytes | Wyoming Piper server source repo — MIT. Текущий Piper runtime `piper1-gpl` — GPL-3.0-or-later; точный состав образа и голосовую модель проверить до распространения. |
| Ollama | `ollama/ollama:latest` | `sha256:dea72d1e22270fe095b68fdf756d2b489d615643c5a2eaebbc5f569181e322d8` | 3,372,404,290 / 2,818,327,922 bytes | Репозиторий Ollama — MIT. В будущей конфигурации использовать digest, не плавающий tag. |

Сжатые слои четырёх образов занимают по registry metadata около 4.70 GB (amd64) или 4.02 GB (arm64). Для первичного планирования пика применяется запас: до 3x от этого числа для распакованных слоёв, одна сжатая копия на время pull, модели, второй экземпляр Ollama model blob, 2 GiB на служебные файлы и отдельный резерв 10 GiB. Операторский каркас поэтому требует минимум 36 GiB свободного места на файловой системе Docker storage. Это оценка для допуска к pull, не измеренный размер распакованных image layers; если фактическая Docker оценка выше, применять её. При 22.7 GiB свободного места подготовка должна остановиться до сетевых загрузок.

## Кандидаты моделей

| Роль | Кандидат и источник | Размер загрузки | Лицензия / проверка |
|---|---|---:|---|
| STT, базовый вариант | `Systran/faster-whisper-tiny`, pinned HF revision `d90ca5fe260221311c53c58e660288d3deb8d356` | 75,538,270 bytes | В HF metadata указана MIT. Мультиязычная Whisper-модель; замерить русскую точность на фиксированном наборе фраз. |
| STT, сравнение качества | `Systran/faster-whisper-small`, pinned HF revision `536b0662742c02347bc0e980a01041f333bce120` | 483,546,902 bytes; `model.bin` SHA-256 `3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671` | В HF metadata указана MIT. Не загружать одновременно с tiny без отдельного решения о сравнительном прогоне. |
| Conversation/tool calling | `qwen3:4b-instruct` в Ollama Library | model blob 2,497,280,480 bytes; manifest `sha256:0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`; model blob `sha256:85e4a5b7b8ef0e48af0e8658f5aaab9c2324c76c1641493f4d1e25fce54b18b9` | В Ollama Library указана Apache-2.0; у семейства Qwen3 есть tool-capable модели. Проверить actual tool-call на HA Ollama integration. Модель 4B — кандидат для замера, не утверждение о достаточном качестве. |
| TTS | `ru_RU-denis-medium` из `rhasspy/piper-voices` | ONNX 63,201,294 bytes; SHA-256 `15fab56e11a097858ee115545d0f697fc2a316c41a291a5362349fb870411b0a`; JSON 4,823 bytes | Карточка Denis указывает CC0 dataset, но голос fine-tuned от Lessac medium. Сверить исходные условия голоса и состав текущего Piper runtime; не делать лицензионных выводов только по repository tag. |

При выборе Whisper small загрузка перечисленных моделей составит около 3.04 GB; tiny вместо small снижает её примерно на 408 MB. Сумма с image layers — около 7.1 GB на amd64 или 6.4 GB на arm64 для одного STT-кандидата. Заложить 10 GB свободного места до скачивания. Двойная установка tiny+small добавит около 76 MB относительно варианта small; реальный файловый объём после распаковки может отличаться.

## Изолированный профиль после отдельного GO

1. Создать чистую конфигурацию HA Core `2026.10.2` в новой QA-папке; не монтировать пользовательский `configuration.yaml`, `.storage`, runtime или credentials. Добавить только `assist_pipeline:` и fixture `light.qa_virtual_light`.
2. Поднять обычные Wyoming Whisper и Piper и Ollama контейнеры в QA Compose. HA подключается к ним по service name. Compose публикует только тестовый HA UI на loopback; тестовые голосовые запросы идут через HA Assist pipeline WebSocket/API, а не через браузерный микрофон.
3. На этапе подготовки загрузить выбранные образы и модели и сверить SHA-256 файлов Whisper small и Piper Denis, а также model blob Qwen3 с указанными digest. После прогрева удалить prepare-контейнеры, затем запустить отдельный runtime Compose profile только с `internal: true` network. Оставить связь между HA, Wyoming, Ollama и локальным harness. Не считать локальную LAN-трафик нулевым: отдельно учитывать байты по каждому внутреннему соединению и WAN egress.
4. Отправить только синтезированный входной звук. Не воспроизводить файлы через динамик, не открывать микрофон, не вызывать wake word. Текст ответа Piper сохранить/проверить в памяти harness; тест не должен проигрывать аудио.
5. На агентском уровне записать выбранный HA conversation agent ID. Для запроса требовать увеличение счётчика Ollama `/api/chat` или локального HTTP proxy counter, увеличение turn counter выбранного Ollama agent и вызов `light.qa_virtual_light`. Если native intent сработал без Ollama request, сценарий считается не пройденным.
6. Зафиксировать ingress/egress byte counters до и после каждого прогона на внешней стороне закрытой сети. Критерий runtime: ноль WAN egress, обращения только к адресам QA Compose, нет вызовов cloud integration. Метрики модели и сервиса писать отдельно.
7. Проверить состояния: normal, missing Whisper model, missing Piper voice, missing Ollama model, Ollama недоступен, Wyoming недоступен, HA холодный restart после прогрева и повторный offline turn. Ошибка должна быть видимой; скрытый cloud fallback не допускается.

### Минимальный синтетический набор

- «Включи тестовую лампу» — STT transcript, явный Ollama request, tool call по `light.qa_virtual_light`, состояние включено, ответ TTS.
- «Выключи тестовую лампу» — обратный tool call и ответ TTS.
- «Сколько будет два плюс два?» — Ollama conversation request без вызова entity tool, ответ TTS.

Для каждого turn записывать timestamp/длительность этапов STT, Ollama first token/full response, tool call, TTS synthesis; ID образов, revision и model blob digest; CPU/RAM peak и WAN/LAN bytes; expected/actual entity state. Тексты команд фиксированы и синтетические, пользовательская история не используется.

## Что пока не проверено

- Точные CPU/RAM/latency требования, пригодность Qwen3 4B для HA tool calls и русской беседы.
- Русская точность выбранного Whisper на конкретном сценарии и голосе.
- Отсутствие WAN-соединений во время будущего реального запуска.
- Restart/missing-model поведение в собранном профиле.
- Лицензионные условия распространения Piper voice и всех зафиксированных образов как цельного дистрибутива.
- Любая работа на реальном оборудовании или с пользовательским домом.

## Официальные источники

- [Issue #4](https://github.com/marsiandeployer/habab-home-assistant/issues/4)
- [Home Assistant: полностью локальный Assist](https://www.home-assistant.io/voice_control/voice_remote_local_assistant/)
- [Home Assistant: Wyoming Protocol](https://www.home-assistant.io/integrations/wyoming/)
- [Home Assistant: Ollama](https://www.home-assistant.io/integrations/ollama/)
- [Home Assistant Core source at 2026.10.2](https://github.com/home-assistant/core/tree/2026.10.2)
- [Wyoming Faster Whisper source and offline/local-files-only mode](https://github.com/OHF-Voice/wyoming-faster-whisper)
- [Wyoming Piper source](https://github.com/OHF-Voice/wyoming-piper)
- [Piper runtime license](https://github.com/OHF-Voice/piper1-gpl)
- [Ollama Docker documentation](https://github.com/ollama/ollama/blob/main/docs/docker.mdx)
- [Ollama Qwen3 library](https://ollama.com/library/qwen3)
- [Ollama Qwen3 4B Instruct model](https://ollama.com/library/qwen3:4b-instruct)
- [Whisper tiny metadata](https://huggingface.co/api/models/Systran/faster-whisper-tiny)
- [Whisper small metadata](https://huggingface.co/api/models/Systran/faster-whisper-small)
- [Piper Russian voice inventory](https://huggingface.co/rhasspy/piper-voices/blob/main/voices.json)
- [Piper Denis model card](https://huggingface.co/rhasspy/piper-voices/blob/main/ru/ru_RU/denis/medium/MODEL_CARD)

Сведения о текущих registry manifests и объёмах моделей получены через metadata endpoints; слои контейнеров, модели и аудио в рамках этой проверки не скачивались.
