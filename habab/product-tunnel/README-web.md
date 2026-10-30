# Home Assistant через исходящий HTTPS/WebSocket-туннель

`web_tunnel.py` содержит relay и Python-агент для одного локального Home
Assistant. Агент подключается к relay через WSS; входящие порты дома не нужны.
Браузер открывает постоянный HTTPS-адрес дома и проходит обычный вход Home
Assistant. Tunnel token удостоверяет агент; он не заменяет пароль или токен HA.

## Запуск

Требуется Python 3.10+ и зависимости из `requirements-web.txt`:

```bash
python3 -m pip install -r habab/product-tunnel/requirements-web.txt
python3 habab/product-tunnel/web_tunnel.py relay \
  --config /private/homes.json --host 127.0.0.1 --port 18330
```

Формат приватного файла relay:

```json
{"homes":{"home-p30.wpmix.net":{"token":"REPLACE_WITH_A_SEPARATE_RANDOM_TOKEN_FOR_THIS_HOME"}}}
```

Оператор генерирует для каждого дома отдельный случайный токен, например
`secrets.token_urlsafe(32)`, и передаёт его агенту приватно. Файлы с токенами
хранятся вне Git с правами владельца. Relay проверяет точное имя дома и токен;
повторное подключение уже подключённого дома получает HTTP 409. Изменение
конфигурации и отзыв токена требуют перезапуска процесса relay; активные
соединения при остановке закрываются.

Relay слушает только loopback. Перед ним нужен HTTPS nginx с проверенным
сертификатом, точным `server_name`, поддержкой WebSocket upgrade на всех путях,
таймаутом чтения не менее 240 секунд и `client_max_body_size 32m`.
`proxy_buffering off` сохраняет передачу первых частей ответа до его завершения;
`proxy_request_buffering off` сохраняет потоковую загрузку. Nginx должен сохранять
исходные Host, Origin, Authorization и Cookie. Его access log не должен записывать
Authorization, Cookie, тело запроса или query с учётными данными. Не открывайте
порт Python relay в публичную сеть.

И браузерные пути HA, и `/_habab_tunnel/control`, и
`/_habab_tunnel/data/<stream>` должны попадать в этот relay. Если перед nginx
стоит ещё один TLS edge, проверьте WebSocket upgrade и таймауты на обоих переходах.
Доменные имена, DNS и сертификаты provision выполняет отдельно.

На машине дома:

```bash
python3 habab/product-tunnel/web_tunnel.py agent \
  --relay wss://home-p30.wpmix.net/_habab_tunnel/control \
  --home home-p30.wpmix.net --token-file /private/home-token
```

По умолчанию агент обращается только к `http://127.0.0.1:8123`. Оператор может
указать другой HTTP origin на буквальном loopback IP через `--target`.
Ни relay, ни запрос из сети не выбирают адрес назначения. HTTP redirects
возвращаются браузеру; агент не следует им. WebSocket redirects блокируются.
Подменённые `Forwarded`, `X-Forwarded-*` и `X-Real-IP` удаляются, поэтому для
этого варианта не требуется добавлять HA trusted proxy. Исходные Host и Origin
сохраняются. HA external URL при необходимости задаётся штатным способом.

Встраивание в существующий asyncio lifecycle:

```python
task = asyncio.create_task(run_agent(
    session, "wss://home-p30.wpmix.net/_habab_tunnel/control",
    "home-p30.wpmix.net", token,
))
# При выгрузке интеграции:
task.cancel()
await asyncio.gather(task, return_exceptions=True)
```

`session` принадлежит вызывающему коду; используйте сессию без HA credentials,
с проверкой TLS и без cookie jar. Для loopback HA агент создаёт собственную
сессию с `DummyCookieJar`: cookies одного браузера не попадают другому.
Параметр `allow_insecure_loopback=True` существует только для локальных тестов;
CLI его не предоставляет.

## Wire protocol v1

Все служебные сообщения — UTF-8 JSON в TEXT-фреймах WebSocket. Один control
socket относится к одному дому. Для каждого HTTP-запроса или браузерного
WebSocket агент открывает отдельный исходящий data socket. HTTP-тела передаются
частями; очередей частей в Python-коде нет, чтение следующей части следует за
ожидаемой записью предыдущей.

Каждый control/data handshake содержит:

```text
Authorization: Bearer <per-home-token>
X-Habab-Home: <exact-provisioned-hostname>
```

1. Агент открывает `wss://relay/_habab_tunnel/control`.
2. Relay отвечает `{"type":"ready"}`.
3. При браузерном запросе relay отправляет
   `{"type":"open","stream":"<48 lowercase hex characters>"}`.
4. Агент открывает WSS к тому же origin,
   `/_habab_tunnel/data/<stream>`, с теми же auth headers.
5. Relay принимает data socket только для существующего stream этого дома,
   текущего control socket и единственного подключения к нему.

Первое сообщение на data socket отправляет relay:

```json
{"type":"request","method":"POST","path":"/auth/token?raw=%2B","headers":[["Host","home-p30.wpmix.net"],["Content-Type","application/json"]],"websocket":false,"protocols":""}
```

`path` — raw origin-form path с query, без декодирования и повторного кодирования.
Агент обязан отклонить абсолютный URL, `//`, обратный слеш, fragment и control
characters. Соединение всегда направлено в заранее заданный локальный HA.
`headers` — массив пар, сохраняющий повторные заголовки. Hop-by-hop headers и
поля, перечисленные в `Connection`, удаляются на обоих концах.

Для HTTP relay передаёт тело BINARY-фреймами до 64 KiB и завершает
`{"type":"end"}`; пустое тело тоже завершается этим сообщением. Агент может
начать ответ до завершения загрузки. Ответ начинается сообщением:

```json
{"type":"response","status":200,"headers":[["Content-Type","text/html"],["Set-Cookie","a=1"],["Set-Cookie","b=2"]]}
```

Затем агент передаёт BINARY-части до 64 KiB и `{"type":"end"}`. Сохраняются
status, повторные Set-Cookie, сжатые байты, Content-Encoding, Content-Length,
ETag, Location и native HA Authorization/Cookie. Не следует автоматически
распаковывать HTTP-тело. Передача HEAD/304 не добавляет отсутствующее тело.

Для WebSocket поле `websocket` равно `true`, а `protocols` содержит исходный
`Sec-WebSocket-Protocol` или пустую строку. HTTP body/end relay не отправляет.
Агент открывает локальный HA WebSocket с предложенными subprotocols; успешный
handshake подтверждает:

```json
{"type":"response","status":101,"protocol":"ha"}
```

Без subprotocol `protocol` равен `null`. После этого в обе стороны:

| Native WebSocket | Data socket |
| --- | --- |
| TEXT | TEXT JSON `{"type":"text","data":"native text"}` |
| BINARY | BINARY с теми же байтами |
| CLOSE | TEXT JSON `{"type":"close","code":1000,"reason":"..."}` |

Control/data ping/pong обслуживает WebSocket-библиотека; они не являются
сообщениями HA. Native ping/pong также обслуживается локально на каждом
переходе. Коды 1005/1006/1015 не отправляются на wire; при потере транспорта
браузер закрывается с 1001. Неуспешный локальный WS handshake возвращает HTTP
status и headers; его тело Python-агент не сохраняет.

## Лимиты и сбои

- До 16 одновременных HTTP/WS streams на дом, 128 на relay; исчерпание даёт 503.
  Долгоживущий WebSocket занимает один stream.
- HTTP request и response — до 32 MiB каждый, с передачей частями по 64 KiB.
  Известный слишком большой request получает 413. Превышение при потоковой
  передаче прерывает stream. Уже начатый ответ обрывается; он не превращается
  в успешный укороченный ответ. Это ограничивает крупные backup/download/upload.
- Native WS message — до 8 MiB. JSON-обёртка тоже ограничена размером data
  WebSocket; текст с большим количеством экранируемых символов может достигнуть
  лимита раньше. Разделение native WS сообщения на несколько сообщений не делается.
- Открытие data socket/локального соединения — до 15 секунд. HTTP request —
  до 180 секунд, ожидание следующей HTTP части — до 180 секунд.
- Control/data heartbeat — 20 секунд. Агент переподключается с задержкой
  1, 2, 4…30 секунд до остановки. Прерванные HTTP/WS streams не повторяются:
  браузер или HA решает, повторять ли операцию.
- Неизвестный браузерный Host получает 421, отключённый дом — 503. Запрещённый
  агент получает 401. Ошибка локального HA или data socket до ответа даёт 502.
- Отмена Python-агента завершает его data tasks. Потеря control закрывает streams
  этого подключения. Код не пишет credentials или URL запросов в application log;
  relay access log отключён.

TLS proxy и relay видят native HA credentials и содержимое HTTP/WS. Relay —
доверенный оператором компонент; этот протокол не добавляет сквозное шифрование
поверх WSS. Токен скомпрометированного агента позволяет ему изображать свой дом;
он не даёт подключиться к другому дому. Адрес loopback контролирует поставленный
агент: модифицированный клиент с собственным токеном может отправлять relay свои
ответы. Биллинг, UI подписки, provisioning и автоматическая ротация сюда не входят.

## Проверка

```bash
.venv/bin/python -m unittest discover -s habab/product-tunnel -p test_web_tunnel.py -v
```

Тесты поднимают только временные aiohttp HTTP/WS fixtures на loopback. Они
проверяют native login/REST, raw paths, сохранение headers/cookies/compression,
передачу static до завершения ответа, WS text/binary/subprotocol/close, отмену,
переподключение, token/host isolation и лимиты. Они не подтверждают публичный
DNS/TLS/edge, реальный HA login flow или установленный Android-клиент.
