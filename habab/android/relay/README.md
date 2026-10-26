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
фактические `configured`/`ready`. Текстовый ключ хранится отдельно в RAM и не
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

Upstream-ключ остаётся в памяти процесса. Сервис не записывает его в конфиг,
переменные окружения или новый файл. После перезапуска требуется повторная
передача ключа; до неё API отвечает 503 `habab_upstream_not_ready`.
Это текущий режим технической проверки владельца. Для постоянной эксплуатации
нужно отдельно решить автоматическое получение ключа из защищённого хранилища.

Обработка запроса означает, что сервер временно получает ключи, текст и аудио
в памяти. Этот режим не выполняет буквальное требование отсутствия секретов
в RAM сервера. Отсутствие новых файлов с секретами и полное отсутствие
секретов на сервере — разные требования. Биллинг и платные права доступа
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
