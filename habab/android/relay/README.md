# API сервиса Habab

Клиент выбирает провайдера Habab и использует отдельный ключ сервиса.
Authenticated `GET /v1/capabilities` объявляет нативный Gemini text API:
`google-generative-language-v1beta`, model `gemini-3.1-flash-lite` и base
`https://habab-voice.wpmix.net/gemini/v1beta`. Запросы идут на
`/models/{model}:generateContent`; потоковый режим использует
`/models/{model}:streamGenerateContent?alt=sse`. Habab credential принимается
в `x-goog-api-key` и заменяется на Google master key только в upstream header.
Для текста relay также предоставляет OpenAI-совместимые `GET /v1/models` и
`POST /v1/chat/completions`, но публикует только managed alias `habab-chat`.
Он направляется на фиксированную модель DeepSeek `deepseek-flash`; upstream
URL нельзя задать через запрос. Auth-защищённый `GET /text/capabilities`
сообщает `protocol=openai_compatible`, `model=habab-chat`, `api_path=/v1` и
фактические `configured`/`ready`. Текстовый ключ хранится отдельно в приватной конфигурации и не
заменяет Google key, используемый голосовым relay. Legacy audio и OpenAI
Realtime endpoints по-прежнему отклоняются кодом `habab_api_upgrade_required`;
Gemini response JSON/SSE передаётся без OpenAI-совместимой обёртки.

Нативный голосовой протокол Habab — Gemini Live: capabilities публикуют
`provider=gemini`, `transport=habab` и разрешённую модель `gemini-3.8-live`.
Для подготовки сессии сервер HA отправляет построенный им `setup` на
`/v1/realtime/bootstrap`; relay выпускает Google constrained token
с `uses=1`, сохраняет его только в RAM и возвращает одноразовый opaque Habab
credential. Устройство подключается к `wss://habab-voice.wpmix.net/gemini/ws`
с `Authorization: Token ...` и обязано отправить первым кадром ровно
`{"setup": ...}`. Relay сверяет setup с bootstrap и проксирует исходные Gemini
JSON и binary frames к constrained Google WebSocket. Google API key и Google
token не попадают на устройство. Для прямого OpenAI или Gemini клиент продолжает
использовать выбранный провайдер напрямую; fallback между ними нет. Старые
Habab OpenAI Realtime маршруты отключены.

Отдельный public demo OpenAI Realtime использует `/openai/demo/ws`, а не
legacy endpoint выше. Он выдаёт только одноразовые grants через приватный
control socket relay и держит OpenAI key на сервере. Приватный relay config
управляет числом запусков в скользящем окне и числом запусков на одну
анонимную demo-сессию:

```json
{"demo_openai_session_limit": 5, "demo_openai_sessions_per_session": 2,
 "demo_openai_session_window_seconds": 86400}
```

По умолчанию разрешено не более пяти запусков за последние 24 часа и двух
запусков на один session hash за те же 24 часа. Ноль отключает запуск.
Истёкшие запуски автоматически освобождают лимит; учёт ведётся в RAM и
сбрасывается при рестарте relay. Это ограниченный QA/demo бюджет, не точная
денежная квота.

Для браузера capabilities добавляют `realtime.browser_auth` с режимом
`first_text_frame` и type `habab.voice.auth.v1`. По умолчанию этот режим
отключён: список точных Origin задаётся полем `browser_origins` приватной
конфигурации relay, например:

```json
{"browser_origins": ["https://habab-demo.wpmix.net"]}
```

Это только пример origin; production-владелец должен указать фактический origin
браузерного Home Assistant. Wildcard, Origin без значения и URL с путём не
принимаются. Клиент без `Authorization` header проходит WebSocket upgrade только
с разрешённым Origin, затем не позднее 10 секунд отправляет один текстовый кадр
размером до 64 KiB строго такой формы:

```json
{"type":"habab.voice.auth.v1","credential":"<opaque relay credential>","setup":{}}
```

WebSocket parser также ограничивает один кадр 256 KiB; кадры больше этого
транспортного лимита могут закрываться стандартным кодом 1009 до проверки
прикладного лимита 64 KiB.

Relay атомарно погашает credential до сравнения setup. Поэтому неверный setup
тоже требует новой bootstrap-сессии. После проверки relay удаляет auth-поля и
отправляет Google только `{"setup": ...}`; opaque credential не попадает в
upstream frame. Неуспешная схема, Origin, replay, expiry или setup не создают
соединение с Google. Browser pre-auth slots учитываются в лимите двух голосовых
соединений. После upgrade политика авторизации закрывает ошибку кодом 1008 с
короткой безопасной причиной; ошибка, transport failure или ненормальное
закрытие upstream закрывается кодом 1011 с безопасной причиной. Нормальные
upstream close codes 1000 и 1001 сохраняются. Native клиенты продолжают
использовать header и setup frame.

## Авторизация и ключи

### Бесплатная бета без подписки

Новая установка Home Assistant сохраняет случайный UUID и запрашивает свой
код через `POST /beta/enroll`. Владелец не вводит ключ AI-провайдера: штатный
config flow сохраняет полученный код как подключение Habab. Уже настроенные
подключения сохраняются. Это требует опубликованного issuer и готовых
текстового и голосового upstream; одних клиентских исходников недостаточно.

У voice, managed text и Exa relay должен быть один приватный `beta_config`
(или `HABAB_BETA_CONFIG`). Создайте его один раз через
`beta_codes.create_config(path)`: каталог 0700, файл 0600. Не помещайте его
в репозиторий и не пересоздавайте при обновлении процесса: `signing_secret`
подтверждает уже выданные коды. Старый `key_sha256` продолжает работать.

Чтобы закрыть регистрацию, измените только `issue_codes_enabled` на `false`
в этом файле, сохранив права и секрет. Issuer читает флаг при каждом запросе,
поэтому перезапуск не требуется. Новые запросы получат 403
`habab_beta_issuance_disabled`; ранее выданные коды продолжат авторизовать
текст, голос, Telegram и Exa. Смена `signing_secret` — отдельная ротация,
а не способ остановить выдачу. Перед открытием регистрации проверьте весь
маршрут с новым кодом и сохранение доступа после выключения флага.

В приватном JSON сервиса хранится `key_sha256` — SHA-256 клиентского ключа
Habab — и `port`. Каталог имеет права 0700, файл — 0600. Ключ Habab не является
Google API key: сервер использует отдельный ключ оператора для upstream. На
клиент возвращаются только ответы API и временный Habab voice credential.

Оператор передаёт upstream-ключ через Unix-сокет `control.sock` в том же
приватном каталоге. Сокет имеет права 0600; процесс проверяет UID отправителя.
`enroll_upstream.py --socket PATH` читает ограниченный JSON из приватного stdin
и выводит только результат операции. Без `--purpose` ключ остаётся Google
upstream key для Gemini text/voice; `--purpose text` записывает отдельный
DeepSeek text key. Не передавайте секрет в аргументах команды, журнале или
файле репозитория.

Рабочий relay сохраняет переданные ключи в `upstream_keys` существующего
приватного `config.json` (каталог 0700, файл 0600) и читает их при запуске.
Атомарная запись завершается до подтверждения enrollment. Ключи не попадают
в Git, командную строку, PM2 environment или клиентские ответы. Если ключ
ещё не задан, сервис явно отвечает, что upstream не готов.

Обработка запроса означает, что сервер временно получает ключи, текст и аудио
в памяти. Этот режим не выполняет буквальное требование отсутствия секретов
в RAM сервера. Ключи провайдеров находятся в приватной серверной конфигурации. Биллинг и платные права доступа
этот сервис пока не реализует.

## Развёртывание

Зависимости закреплены в `requirements.txt`. Приложение запускается через
`ecosystem.config.cjs` в отдельном venv и слушает только loopback.
`nginx.conf` описывает внутренний reverse proxy, `nginx-edge.conf` — TLS edge.
Перед применением проверьте адреса, пути и `nginx -t`. Access log отключён
на обоих уровнях; заголовки, тела и WebSocket-кадры не журналируются.

Habab voice credential хранится как SHA-256 до 60 секунд, привязан к Gemini
модели и setup, и погашается при первом подключении. Соответствующий Google
constrained token хранится в RAM до подключения и передаётся только upstream.
Лимиты: два голосовых соединения, 180 секунд и 16 MiB в каждую сторону;
шесть параллельных HTTP-запросов, 2 MiB входящего и 8 MiB ответного тела.

Если выбран Habab, бот может обращаться через Bot API relay с авторизацией.
`GET /v1/capabilities` объявляет `telegram.protocol=telegram-bot-api-relay-v1`,
`base=/telegram` и точный список поддержанных методов. Каждый вызов требует
`Authorization: Bearer <HABAB_SERVICE_KEY>` и
`X-Habab-Telegram-Bot-Token: <BOT_TOKEN>`. Gateway проверяет service key до
обращения к Telegram, затем проверяет формат Bot API token. Этот token передаётся
Telegram Bot API только в фиксированном URL
`https://api.telegram.org/bot<TOKEN>/<METHOD>`; произвольный URL и произвольные
методы проксировать нельзя. Ошибки и access log не содержат token.

Разрешены `GET /telegram/getMe` без query/body и `POST /telegram/getMe` с JSON
`{}`. Остальные поддержанные методы используют `POST` с JSON:
`getUpdates`, `getChatMember`, `sendMessage`, `sendChatAction`. Query параметры,
другие методы и другие HTTP verbs отклоняются до upstream. `getUpdates` — один
upstream-вызов на каждый запрос клиента, с `timeout` от 0 до 25 секунд; сервер
не запускает фоновый poller. При отмене клиентского запроса отменяется и
upstream-вызов. Gateway использует общий лимит шести HTTP-запросов, ограничивает
входное тело 1 MiB, ответ 8 MiB и не следует redirect. Успешный Bot API JSON,
HTTP status, включая 429 с `retry_after`, передаются клиенту. Timeout, неверный
ответ, превышение лимита и upstream-сбой возвращают безопасный код ошибки.
Google master key для Telegram не используется.

Проверки без реальных ключей:

```sh
python -m unittest discover -s habab/android/relay -p test_gateway.py -v
```

Текущий процесс — `habab-voice-relay`, loopback 8326. Его приватный конфиг,
venv и исходники являются активными зависимостями, которые нужно сохранять
при очистке диска.

## Серверная статистика расхода

Рабочий managed-text relay пишет числовой журнал в `usage/YYYY-MM-DD.jsonl`
рядом с приватным config.json. Каталог имеет права 0700, файлы — 0600.
Установка обозначается SHA-256 её кода Habab; это идентификатор установки,
а не имя человека. Ключи, запросы, ответы и аудио в журнал не записываются.

Для текста сохраняются фактические provider/model, результат, время запроса
и поля usage DeepSeek: входные, выходные, общие и доступные cache-токены.
Потоковые ответы разбираются по SSE, включая фрагментацию сетевых блоков.
Отсутствующий usage остаётся неизвестным; в отчёте он виден как missing_usage.
Токены доступны только для запросов через сервер Habab: прямые подключения
пользователя к провайдерам сервер не наблюдает.

Канал voice_activation учитывает подключения к Gemini Live и их ошибки.
Его requests — попытки активации, а не число произнесённых реплик; токены
голоса пока не учитываются. Стоимость по тарифам не рассчитывается.

Сводка по всем сохранённым датам, установкам, каналам и моделям:

```sh
python3 usage_stats.py /root/.local/share/habab-text-relay/usage
```

После перезапуска процесса существующие журналы продолжаются.

## Health и уведомления владельцу

`GET /health` проверяет, что процесс отвечает. Новый `GET /health/ready`
возвращает HTTP 200 только при готовности проверок текста и голоса; при сбое —
HTTP 503 с причиной по каждому провайдеру. Для публичного адреса включите
`nginx-health-location.conf` в server-блок habab-voice.wpmix.net.

Проверка ключей обращается к бесплатному списку моделей раз в 60 секунд.
`probe=credential_access` означает проверку доступа, а не оплачиваемый
генеративный запрос. `last_inference_status` отдельно показывает результат
последнего настоящего запроса. Успешный список моделей не отменяет ранее
полученную ошибку inference или HTTP 429; для восстановления нужен успешный
настоящий запрос. Причины: missing_credential, credential_rejected,
rate_limited, provider_unavailable, stale_check. HTTP 429 не доказывает, что
закончился баланс: причиной может быть частота запросов.

Для сообщений владельцу приватный config.json принимает
`health_notify_command` (массив пути Python и существующего telegram_sender.py)
и `health_notify_recipient`. Команда запускается без shell, с отключённым
логированием содержимого Telegram. Одинаковая причина повторно не отправляется,
состояние сохраняется в health-state.json; после восстановления приходит новое
уведомление. Успешная доставка подтверждается message_id отправителя,
одного нулевого exit code недостаточно. Тексты и ключи AI-провайдеров в
уведомления не попадают.

Для диагностики запинок можно включить `voice_diagnostics: true` в приватной
конфигурации voice relay. `HababVoiceWire` пишет только события setup, VAD,
прерывания и завершения, число аудиопакетов, максимальную паузу между ними и
число аудиопакетов с флагом thought. Идентификатор трассы случайный, не связан
с ключом или пользователем. Максимум 128 событий на соединение; текст, PCM,
провайдерские идентификаторы и credentials не выводятся. По умолчанию запись
выключена. Эта трасса сама по себе не доказывает эхо или слышимую запинку:
сопоставляйте её с числовым журналом Android и наблюдением пользователя.
