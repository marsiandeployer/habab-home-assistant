# API сервиса Habab

Клиент выбирает провайдера Habab и использует отдельный ключ сервиса.
Authenticated `GET /v1/capabilities` объявляет нативный Gemini text API:
`google-generative-language-v1beta`, model `gemini-3.1-flash-lite` и base
`https://habab-voice.wpmix.net/gemini/v1beta`. Запросы идут на
`/models/{model}:generateContent`; потоковый режим использует
`/models/{model}:streamGenerateContent?alt=sse`. Habab credential принимается
в `x-goog-api-key` и заменяется на Google master key только в upstream header.
OpenAI-совместимые `/v1/chat/completions`, models и audio endpoints явно
отклоняются кодом `habab_api_upgrade_required`; Gemini response JSON/SSE
передаётся без OpenAI-совместимой обёртки.

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

## Авторизация и ключи

В приватном JSON сервиса хранится `key_sha256` — SHA-256 клиентского ключа
Habab — и `port`. Каталог имеет права 0700, файл — 0600. Ключ Habab не является
Google API key: сервер использует отдельный ключ оператора для upstream. На
клиент возвращаются только ответы API и временный Habab voice credential.

Оператор передаёт upstream-ключ через Unix-сокет `control.sock` в том же
приватном каталоге. Сокет имеет права 0600; процесс проверяет UID отправителя.
`enroll_upstream.py --socket PATH` читает ограниченный JSON из приватного stdin
и выводит только результат операции. Не передавайте секрет в аргументах
команды, журнале или файле репозитория.

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

Telegram использует прямой Bot API и отдельный необязательный пользовательский
HTTP-прокси в настройках бота. Сервер Habab не запускает Telegram poller
и не предоставляет API-пересылку Telegram.

Проверки без реальных ключей:

```sh
python -m unittest discover -s habab/android/relay -p test_gateway.py -v
```

Текущий процесс — `habab-voice-relay`, loopback 8326. Его приватный конфиг,
venv и исходники являются активными зависимостями, которые нужно сохранять
при очистке диска.
