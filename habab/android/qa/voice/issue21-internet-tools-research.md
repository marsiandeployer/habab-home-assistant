# Issue #21: OpenCode-доступ к интернету из чата и голоса

Статус: исследование исходников и актуализация после реализации d2f8090e2. В ходе исследования OpenCode не запускался для сетевых запросов, ключи/API не использовались. Реальная проверка инструментов из облачного чата и APK ещё не выполнена.

## Вывод

В закреплённом OpenCode `1.14.21` оба нужных инструмента уже встроены. Для текущего кастомного Habab-провайдера пользователь выбрал полный YOLO: `permission: {"*":"allow"}`. Исходник SetupAgent уже применяет эту политику и передаёт `OPENCODE_ENABLE_EXA=1` в среду дочернего процесса. В pinned версии `websearch` вызывает встроенный Exa MCP. Код использует endpoint без ключа, если `EXA_API_KEY` не задан; отдельный Exa key для доступности инструмента не требуется по коду этого тега. Это не подтверждает тариф, доступность сервиса или фактический вызов инструментом.

Голос уже передаёт просьбы «проверь сайт/веб-задачу» в общий OpenCode-исполнитель. После включения те же инструменты доступны обоим интерфейсам; отдельный Android HTTP-клиент не нужен. Наличие инструментов само по себе не заставляет модель искать: инструкции должны направлять актуальные вопросы и проверку сайтов в поиск/загрузку источника.

## Подтверждено исходниками проекта

- Бинарник закреплён как `1.14.21` для amd64 и arm64 в [opencode-release.json](../../../distribution/opencode-release.json:1).
- До реализации `SetupAgent.ensure()` ограничивал инструменты общим deny. В d2f8090e2 по прямому указанию пользователя установлен `permission: {"*":"allow"}` и `OPENCODE_ENABLE_EXA=1`; запуск остаётся `serve --pure` в отдельном runtime. Это проверено в текущем [setup_agent.py](../../../distribution/src/habab_home/setup_agent.py) и [test_setup_agent.py](../../../distribution/tests/test_setup_agent.py). Установленная Native16 по-прежнему использует старый runtime.
- Дочерний процесс получает специально собранную среду: родительскими переменными ограничены `PATH`, `LANG`, `TZ`, `SSL_CERT_FILE`, затем добавляются собственные XDG-пути, выбранный provider key и внутренние токены ([setup_agent.py:232–243](../../../distribution/src/habab_home/setup_agent.py:232)). `HTTP_PROXY`, `HTTPS_PROXY` и `NO_PROXY` не передаются. Прокси, настроенный только в родительской среде, дочерний OpenCode не унаследует.
- Выбранный облачный provider настраивается отдельно от web tools ([setup_agent.py:198–228](../../../distribution/src/habab_home/setup_agent.py:198)). Доступ модели к provider не подтверждает исходящий доступ инструментов.
- `RealtimeTurnView` принимает текст вызова голосового инструмента и вызывает `assistant.run(..., "web", ...)` ([realtime.py:560–618](../../../distribution/src/habab_home/realtime.py:560)); web-канал направляется в общий `runtime.run` OpenCode ([assistant.py:247–270](../../../distribution/src/habab_home/assistant.py:247)). Realtime prompt включает проверку сайта/веб-задачи в поводы вызвать `ask_home_assistant` ([realtime.py:36–48](../../../distribution/src/habab_home/realtime.py:36)).
- Имеющаяся гостевая HTTPS-проверка дала `api.met.no` HTTP 200; отдельная попытка к Telegram завершилась IPv4 TCP timeout ([native16-guest-network-preflight.json:2–20](native16-guest-network-preflight.json:2), [native16-guest-telegram-ipv4-tls-readback.json:1–10](native16-guest-telegram-ipv4-tls-readback.json:1)). Это не проверяло OpenCode→Exa, fetch сайта или доставку результата на телефон.

## Фактическая поддержка в OpenCode 1.14.21

- Pinned registry содержит `webfetch` и `websearch`; `websearch` передаётся модели только для OpenCode provider **или** при `OPENCODE_ENABLE_EXA` ([registry.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/registry.ts#L101-L109), [условие выбора](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/registry.ts#L254-L264)). Флаг принимает `1` или `true` ([flag.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/flag/flag.ts#L2-L5), [#L63](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/flag/flag.ts#L63)). Habab использует кастомный provider `habab-{provider}`, значит для него нужен этот флаг.
- В теге `v1.14.21` есть один websearch backend: Exa MCP. Инструмент вызывает `web_search_exa` на endpoint `https://mcp.exa.ai/mcp`, с timeout 25 секунд ([websearch.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/websearch.ts#L24-L65), [mcp-exa.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/mcp-exa.ts#L49-L72)). Актуальная документация описывает больше search providers и другую схему; эти новые поля и ключи не следует считать доступными в закреплённом бинарнике.
- `EXA_API_KEY` опционален в исходнике этого тега: при наличии значение добавляется в URL как query parameter, а без него используется endpoint без ключа ([mcp-exa.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/mcp-exa.ts#L3-L5)). Для начальной настройки не требуется брать чей-либо ключ. Источник не устанавливает тариф, квоты или условия обработки данных сервиса.
- `webfetch` независимо от Exa-флага загружает HTTP(S) URL. Исходник задаёт limit 5 MiB, timeout по умолчанию 30 секунд и максимум 120 секунд ([webfetch.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/webfetch.ts#L8-L43), [HTTP fetch](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/webfetch.ts#L61-L93)). Это fetch, а не браузер: JS-рендеринга нет. В проверенном tool-файле нет host allowlist; надо отдельно проверить адреса локальных/частных сетей и redirect behavior в точном бинарнике.
- Каждый инструмент запрашивает permission: поисковый запрос для `websearch`, URL для `webfetch` ([websearch.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/websearch.ts#L35-L46), [webfetch.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/tool/webfetch.ts#L27-L41)). Схема 1.14.21 поддерживает конкретные ключи `webfetch` и `websearch` ([config/permission.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/config/permission.ts#L18-L48)); evaluator использует последнее совпадающее правило, поэтому специфичный allow переопределяет общий deny ([evaluate.ts](https://github.com/anomalyco/opencode/blob/v1.14.21/packages/opencode/src/permission/evaluate.ts#L8-L13)). Первоначально исследовалась узкая схема allow поверх deny; последующее прямое указание пользователя заменило её полным `* allow`.
- Официальная [текущая документация по network](https://docs.opencode.ai/docs/network/) описывает `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `NODE_EXTRA_CA_CERTS`. Это актуальное общее руководство, не подтверждение поведения каждой переменной в закреплённом 1.14.21. Локально подтверждено только, что `SetupAgent` эти переменные сейчас не передаёт. Не передавать автоматически произвольные proxy env хоста.

## Принятая реализация по указанию пользователя

Ожидаемая схема permissions:

```json
"permission": {"*": "allow"}
```

В исходнике уже передан `OPENCODE_ENABLE_EXA=1` только в env процесса OpenCode. Не включать общий `OPENCODE_EXPERIMENTAL`, так как он включает другие экспериментальные возможности. Не задавать `EXA_API_KEY` без отдельной причины. Если понадобится прокси, брать только явно выбранные переменные из доверенной настройки guest/runtime и отдельно подтвердить совместимость их транспортом 1.14.21; текущий env allowlist оставлять изолированным от host keys/HOME.

Инструкции для общего и голосового помощника должны направлять свежие сведения и проверку внешнего сайта к `websearch`, а найденную первичную страницу — к `webfetch`. Ответ должен указывать использованный источник и честно сообщать о недоступности поиска. Веб-страницы и результаты поиска — недоверенный внешний ввод; не исполнять из них инструкции и не отправлять в запросах ключи, токены или приватные данные дома.

## Ограничения и непроверенное

- `websearch` отправляет текст запроса на Exa endpoint. Не включать в запросы секреты и лишние личные/домашние данные. В исходнике виден адрес и формат запроса, но не условия хранения данных.
- `webfetch: allow` даёт модели возможность запросить любой HTTP(S) адрес, достижимый из runtime. Проверенный tool-файл не показывает локальный host allowlist; безопасность адресации и redirect цепочек надо подтвердить отдельно на точной сборке, при проверке точной сборки. Общая разрешающая политика уже явно выбрана пользователем.
- Успешный HTTPS к одному публичному host из guest не гарантирует доступ Exa, сайтов с bot challenge, всех DNS/IPv6 маршрутов или доставку результата через UI. Telegram timeout — отдельный сетевой исход, не доказательство блокировки HTTPS веб-поиска.
- Нет независимой trace реального вызова OpenCode web tool из чата/голоса. Здесь не было OpenCode-сетевых запросов, model calls, ключей, установок или телефонных действий.

## Два сценария приёмки после реализации

1. **Чат — известная страница.** Запросить в Habab проверить конкретную публичную страницу и извлечь указанный факт. Независимая trace подтверждает выбранные OpenCode session/provider, вызов `webfetch` с целевым host, завершённый HTTP fetch, цитируемый результат и совпадающий ответ в чате. Само заявление модели «проверил» не считается доказательством.
2. **Голос — поиск свежих данных.** Спросить свежую публичную информацию без URL. Trace подтверждает Realtime `ask_home_assistant` → общий OpenCode session → `websearch` (Exa), затем при необходимости `webfetch`, возврат источника в тот же голосовой сеанс и запись реплики в общий журнал. Проверить, что был реальный вызов инструмента, не ответ модели из памяти.

В trace достаточно timestamp/session ID/provider/model/tool name, host URL или нормализованного query hash, HTTP status/latency и результата доставки в UI/голосовой журнал. Не сохранять секреты, аудио или сырые личные запросы.

## Источники

- Локальный pinned manifest: `habab/distribution/opencode-release.json`.
- Исходники проекта: `habab/distribution/src/habab_home/setup_agent.py:190–249`, `realtime.py:36–48,560–618`, `assistant.py:247–270`.
- OpenCode official primary source tag `v1.14.21`: ссылки `registry.ts`, `websearch.ts`, `mcp-exa.ts`, `webfetch.ts`, `config/permission.ts` выше.
- OpenCode current docs, не pin-specific: [Tools](https://docs.opencode.ai/docs/tools/), [Network](https://docs.opencode.ai/docs/network/).
