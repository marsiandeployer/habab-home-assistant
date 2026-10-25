# I-Digital SMS для Habab Home

## Решение

Добавить отдельную интеграцию Home Assistant `idigital_sms` с UI config flow и
явным сервисом `idigital_sms.send_sms`. Сервис принимает один номер и один
текст; автоматизации вызывают его как обычное действие HA. Web chat и Telegram
уже используют общий Habab AssistantService и одну action-dispatch точку
`setup_actions.execute`; добавить SMS в этот путь и вызвать тот же обработчик,
что и HA service. Не вводить второй assistant или параллельный SMS tool.
Нативный HA Assist остаётся отдельным входом и в первом срезе не получает
отдельного SMS tool; будущая поддержка должна входить через тот же
admission/confirmation и send handler.

Это подходит лучше REST notify: тот настраивается в `configuration.yaml`,
отправляет фиксированный REST запрос и не преобразует ответ Direct в историю
состояний «принято / доставлено / ошибка». Современное `notify.send_message`
адресует notify entity и принимает `message`/`title`; у него нет поля для
произвольного номера телефона. Старый `notify.<name>` принимает `target`, но
Core помечает старые действия на миграцию. Для требуемого номера на каждый
вызов собственное действие `idigital_sms.send_sms` выражает получателя явно и
сохраняет место для проверки, лимита и результата провайдера.

Источники интерфейсов: [RESTful Notifications](https://www.home-assistant.io/integrations/notify.rest/),
[Send a notification message](https://www.home-assistant.io/actions/notify.send_message/),
[notify entity transition](https://developers.home-assistant.io/blog/2024/04/10/new-notify-entity-platform/),
[notify legacy service в Core 2026.10.2](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/notify/legacy.py).

## Что уже есть

- В `habab_home/config_flow.py` API-ключ вводится селектором типа password;
  ключ не возвращается в suggested values. Этот шаблон подходит для отдельного
  flow. В локальном `habab/distribution/README.md` указано, что config entries
  хранятся в `/config` и входят в резервные копии. Не называть это шифрованием
  на диске: доступ к резервной копии должен быть ограничен.
- В `habab_home/integration_setup.py` и Setup chat уже есть `start_integration`:
  владелец начинает штатный flow в чате, а пароль вводит в защищённой форме.
  Flow настраивает соединение; сам по себе он ничего не отправляет.
- Web и Telegram входят в один `habab_home/assistant.py:AssistantService.run`;
  `habab_telegram/__init__.py` передаёт канал `telegram`, web использует тот же
  runtime. Оба вызывают локальный action endpoint, чей dispatcher —
  `habab_home/setup_actions.py:execute`. Нативный Assist в
  `habab_home/conversation.py` вызывает `AssistantService.run_assist` отдельно.
  Поэтому чатный send/confirm должен использовать общий dispatcher и один
  admission/отправочный обработчик; отдельный Assist tool допустим только как
  тонкий вход в него.
- SMSforCRM уже содержит отправку, нормализацию номера, оценку частей и запрос
  статуса: `/root/smsforcrm/backend/idigital.py` (`send_message_with_key`,
  `query_status_with_key`, `get_balance`), `phone.py` (`validate`), `sms.py`
  (`count_parts`, `precheck`, проверки общего имени). `medods.py` и
  `storage.py:claim_medods_reminder` резервируют отправку перед вызовом провайдера
  и не повторяют запрос после неоднозначного результата.
- Текущий `/root/smsforcrm/backend/idigital.py` нельзя импортировать в HA как
  самостоятельный модуль без CRM-зависимостей: он импортирует `config` и
  `storage`, берёт API base из приложения и использует его HTTP-клиент. Это
  подтверждает границу повторного использования, но не требует сначала
  рефакторить и публиковать общий package. Для первой HA-версии держать узкий
  клиентский adapter за интерфейсом `send/status`, с передаваемым transport и
  теми же проверенными полями запроса и отображением статусов, что
  `/root/smsforcrm/backend/idigital.py`. Это обратимо: после первого HA slice
  можно вынести общую чистую HTTP/auth/status часть, если её можно импортировать
  без CRM storage/config. Не создавать копию CRM истории и не блокировать HA
  интеграцию на package release.

## Настройка владельцем

Путь: Home Assistant → **Settings → Devices & services → Add integration →
I-Digital SMS**. В форме запросить API-ключ типа для работы с сообщениями и имя
отправителя. Ключ хранить только в `ConfigEntry.data`, поле вводить как пароль,
никогда не помещать в prompt, событие, лог, ошибку или повторно возвращаемое
значение формы. Обновление/переавторизация принимает новый ключ без показа
старого.

Для проверки flow без SMS использовать `/balance`, затем безвредный запрос
несуществующего UUID через endpoint статуса: баланс принимает любой тип ключа и
сам не доказывает право на работу с сообщениями. Ошибку 404 именно от запроса
случайного UUID можно считать подтверждением доступа к message API; остальные
ошибки показывать только безопасным кодом. Имя отправителя сохранить после
синтаксической проверки; его фактическое разрешение доказывает только ответ
провайдера на отправку, а flow не должен отправлять тестовое сообщение.

И-Digital использует Basic auth с уже Base64-ключом `key_id:password`; повторно
кодировать его нельзя. Вызов выполняется только с сервера: документация
провайдера прямо запрещает использовать Direct API из браузера. Не проксировать
ключ через фронтенд или Habab public demo.

## Сервис и статусы

`idigital_sms.send_sms` schema:

```yaml
target: "+7…"       # обязательно, ровно один мобильный номер
message: "…"         # обязательно, непустой текст
config_entry_id: "…" # обязательно, точная запись I-Digital
```

Проверить `target` через общий `phone.validate`, обрезать только внешние пробелы
у сообщения, отклонять пустой текст, несколько получателей и превышение
локального ограничения длины/частей до HTTP вызова. Формировать одно сообщение:
`channelType=SMS`, сохранённый `senderName`, E.164 destination без `+` и UTF-8
content. Передать его через общий клиент SMSforCRM. Начальный ответ
`errors=false` и `messageUuid` означает только **принято провайдером**; это не
означает доставку.

Интеграция возвращает service response с `status: accepted` и `message_id`.
Отдельное действие `idigital_sms.get_status` получает `message_id`, использует
`query_status_with_key` и возвращает один из безопасных итогов:

- `accepted` — запрос создан, конечная доставка ещё не подтверждена;
- `delivered` — Direct сообщил `delivered`;
- `error` — Direct сообщил финальный неуспешный статус;
- `pending` — ID пока не виден или Direct ещё не сообщил финальный статус;
- `unknown` — ответ не удалось надёжно сопоставить с известным состоянием.

Официальный GET `/message/{message_uuid}` принимает также `externalMessageId`;
документация предупреждает, что в первые 10 секунд после отправки может прийти
404. Этот ответ переводится в `pending`, а не в ошибку доставки. Опрос статуса
можно запускать из automation после паузы или из UI действия HA; не запускать
фоновый постоянный опрос. Непонятный статус Direct отображать как `unknown`.

Источники: [авторизация и типы ключей](https://api.docs.direct.i-dgtl.ru/authorization/),
[POST SMS и `errors=false` / `messageUuid`](https://api.docs.direct.i-dgtl.ru/messages/sms-sending/),
[GET сообщения по UUID](https://api.docs.direct.i-dgtl.ru/messages/get/).

## Чаты и подтверждение

Новый LLM platform/tool не нужен для первого среза. Habab web chat и Telegram
уже идут через общий assistant и `setup_actions.execute`; добавить в этот
dispatcher только SMS-specific request/confirm actions, которые вызывают одну
общую функцию. Нативный HA Assist использует отдельный путь
`conversation.py` → `AssistantService.run_assist`; не добавлять ему отдельный
SMS LLM tool в этом срезе. Сначала обеспечить такой же явный confirmation gate
там, либо оставить SMS недоступным из native Assist.

Перед отправкой web chat и Telegram показывают точные номер и текст и требуют
отдельного подтверждения в том же канале. Создать короткоживущую одноразовую
заявку в памяти, привязанную к owner, mapped conversation/channel, Telegram
bot/chat и исходному участнику (если действует group ACL), точным номеру/тексту
и случайному nonce. Не писать pending SMS text/phone в persistent Store или лог;
после перезапуска HA незавершённая заявка истекает. Подтверждение принимается
только через текущий web/Telegram ingress после повторной проверки действующих
owner/group ACL и равенства nonce; nonce/request ID атомарно помечается
использованным до единственного provider POST. Для Telegram использовать тот
же существующий owner и group binding; не считать сам ответ «да» достаточным
без ожидающей заявки. Не отправлять сообщение в чат до подтверждения точных
значений.

Общая admission-функция проверяет active owner/session, исходный канал и
principal, pending request, совпадающий nonce, неистёкший срок и неиспользованный
request ID. Web и Telegram используют уже существующие session mapping и
owner/group ACL: `AssistantService.trusted_origin` разрешает origin текущего
runtime session, а Telegram `_turn_can_send` повторно проверяет owner/member
binding перед ответом. SMS action обязан передать и повторно проверить эти
данные; нельзя полагаться только на `Context(user_id=owner)`.

Существующий `setup_actions.execute` принимает общий `call_service` после
проверки, что service существует, и вызывает HA service от имени owner; этот
ветвящийся путь не проверяет SMS confirmation или Telegram group principal.
Поэтому SMS нельзя оставлять доступным через общий `call_service`: исключить
`idigital_sms.send_sms` из него и добавить узкий request/confirm action с
проверкой origin. После подтверждения внутренний handler вызывает ту же
send-logic, что и HA service для automation. Config Flow защищает API key;
он не является обязательной формой-посредником для chat отправок.

Существующий `setup_actions.execute` принимает общий `call_service` после
проверки, что service существует, и вызывает HA service от имени owner; в коде
нет SMS-специфичного подтверждения или ACL. Поэтому не добавлять туда прямой
разрешённый путь `call_service(idigital_sms.send_sms)`. Для SMS перехватывать
вызов в том же action dispatcher и направлять его в общую заявку/подтверждение;
либо исключить `idigital_sms.send_sms` из общего маршрута и документировать
ограничение до появления такого gate. После отдельного подтверждения внутренний
handler вызывает тот же service logic, что и автоматизации.

## Ограничения отправки и повторов

- Ровно один recipient на вызов; не поддерживать списки, рассылки или batch API.
- Ввести последовательную отправку и предложенные локальные лимиты: не чаще
  1 SMS в минуту и не более 10 SMS в час на ConfigEntry. Это проектные safety
  defaults, не лимиты I-Digital; подтвердить их до реализации, вынести в
  константы и сообщать о блокировке до API вызова.
- После явного подтверждения присваивать попытке уникальный request ID и
  резервировать его в ограниченном HA `Store` до сетевого запроса. Хранить
  только ID, время, состояние и provider UUID; не хранить текст, номер или
  API-ключ. Дубликат того же request ID возвращает предыдущий результат и не
  отправляет повторно. Pending chat request с точным text/phone держать только
  в памяти до короткого TTL; лимитировать размер и срок хранения истории
  попыток.
- Не делать retry POST `/message` после timeout, сброса соединения или
  невалидного ответа: провайдер мог принять сообщение. Состояние становится
  `unknown`, request ID остаётся использованным. Повтор возможен только как новое
  действие с явным новым подтверждением. `externalMessageId` годится для поиска
  статуса, но документация не обещает дедупликацию по этому полю.
- Логировать только operation ID и безопасный код ошибки. Не выводить key,
  номер, текст, полный provider body или исключение клиента. Не использовать
  динамический callback URL: локальная установка HA не обязана иметь доступный
  I-Digital endpoint. Для DLR использовать status GET.

## Тесты и границы приёмки

**Unit:** valid/invalid ключ и sender form; отсутствие API key в suggested values,
ошибках и логах; тест connection flow без SMS; телефон/пустой текст/несколько
targets; точный payload одного сообщения; provider rejection mapping;
`errors=false` → `accepted`, `delivered` отдельно, финальные ошибки,
неизвестные статусы и немедленный 404 → `pending`; request-ID повтор не вызывает
второй POST; timeout оставляет `unknown` и повтор с тем же ID не вызывает POST;
fake clock проверяет cooldown/hourly cap; web/Telegram request с точным text/phone,
confirmation от чужой сессии или участника, истёкший/повторный nonce, смена
Telegram owner/group binding и попытка обойти gate через общий `call_service` не
отправляют SMS; успешное подтверждение вызывает ровно один POST. При рестарте
незавершённый request недоступен.
Все сетевые запросы подменить локальным fake transport.

**Home Assistant integration:** Flow UI → сохранение entry → service schema в
Tools/Actions → service response `accepted` → status action показывает отдельный
`delivered` либо `error`. Проверить одинаковую общую отправочную функцию через
HA action и чатный confirmation path. Native Assist не считается поддержанным,
пока его отдельный вход не использует тот же confirmation gate. Не считать тест
SMSforCRM доказательством интеграции HA.

**Provider E2E — отдельное разрешение перед выполнением:** сначала владелец
проверяет в личном кабинете тип ключа, остаток бонусного баланса, sender и
зарегистрированный номер; настройки провайдера не менять. Один ранее
разрешённый SMS по задаче уже отправлен и доставлен; этот план не инициирует
повтор. Для любого следующего provider E2E использовать только отдельно
подтверждённый один номер и точный текст, затем проверить `messageUuid` и
provider status. После проверки запросить подтверждение получения у владельца.

## Проверка утверждения о бесплатной отправке

Публичная справка I-Digital сейчас описывает 50 бонусных рублей при регистрации;
в тестовом режиме отправка доступна только на телефон, указанный при регистрации
аккаунта. Другая страница называет ориентир около пяти SMS. Это подтверждает
бонусный тестовый режим с ограниченным адресатом, но не постоянный тариф
«бесплатно на один номер». Не переносить эту фразу в интерфейс или документацию
как гарантию. Фактические остаток бонусов, режим аккаунта и номер из приватного
QA-файла здесь не проверялись.

Источники: [тестовый режим и номер регистрации](https://faq.docs.direct.i-dgtl.ru/bystryi-start/prokhozhdenie-onbordinga-i-testovaya-otpravka/),
[бонусные 50 рублей / примерное число SMS](https://faq.docs.direct.i-dgtl.ru/rabota-s-rassylkami/obshie-voprosy-po-rabote-s-servisom/kak-mozhno-protestirovat-servis-eto-besplatno/).

## Точки кода для следующей реализации

Новая интеграция: `habab/distribution/src/idigital_sms/` с
`manifest.json`, `config_flow.py`, `__init__.py`, `services.yaml`,
`strings.json`, `translations/ru.json`, `llm.py`, плюс tests под
`habab/distribution/tests/`. Перед кодом зафиксировать установку общего
`idigital_direct` package и проверить, что сервис response и LLM platform доступны
на pinned Core `2026.10.2`. Никакие правки provider entry `habab_home`, web
provider options, live HA конфиг и SMSforCRM production настройки в этот план не
входят.
