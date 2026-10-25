# I-Digital SMS для Habab Home

## Проверка нативного REST пути (2026-10-25)

До отдельной интеграции проверен встроенный `rest_command`. Он принимает
шаблонизируемые URL, заголовки и payload и отдаёт automation response с HTTP
status/body. Поэтому REST-команда может отправить ровно один Direct API объект
и отдельным GET прочитать статус по UUID. Копируемый рецепт и пределы описаны
в [SMS_REST_SETUP.md](SMS_REST_SETUP.md), YAML находится в
[`sms_rest_commands.yaml`](sms_rest_commands.yaml).

На момент этой проверки REST recipe покрывал только ручную отправку и простые
автоматизации с ограниченным операторским процессом. В нём не было UI
настройки, защиты от повторов, лимита частоты, подтверждения текста и состояния
попытки после сетевого таймаута. Текущий кандидат описан ниже; этот раздел
сохраняет сравнение предыдущего REST варианта.

## Текущий кандидат (2026-10-25)

Добавлен source candidate `idigital_sms` с локальным config flow и одной
notify entity на запись. `Dockerfile` копирует компонент в Core image, а
`habab_home.setup_actions` включает его в список встроенных config flow. Это
подтверждено офлайн-загрузкой на закреплённом Core image и тестами каталога;
это не релиз и не provider E2E.

Владелец задаёт один фиксированный номер при настройке. Автоматизации и
встроенный помощник используют `notify.send_message` с текстом и выбранной SMS
entity. `idigital_sms.get_status` выполняет отдельный GET по UUID, который
возвращается после ответа Direct. Штатный notify state означает время
последнего принятого вызова, а UUID доступен атрибутом entity; ни то, ни другое
не доказывает доставку.

Установленный кандидат не проверяет ключ запросом к провайдеру, не отправляет
тестовое SMS и не связывает отправку с web/Telegram чатом или native Assist.
Один service call делает не более одного POST, но отдельные повторные вызовы не
дедуплицируются и лимита частоты пока нет. Timeout даёт неопределённый исход и
не повторяется автоматически. SMS delivery и получение владельцем не проверены.

`notify.rest` также не выбран для этого минимального пути: его штатные поля —
message/title/target и дополнительная map `data`, а Direct SMS API требует
массив объектов запроса и возвращает UUID, нужный для дальнейшей проверки
статуса. `rest_command` подходит к форме API и позволяет automation обработать
ответ. Официальный переход HA к notify entities пока описывает только
`notify.send_message` с `message`; recipient-support остаётся отдельной задачей.

Штатные SMS-интеграции, рассмотренные для сравнения, — [ClickSend SMS](https://www.home-assistant.io/integrations/clicksend)
и [Twilio SMS](https://www.home-assistant.io/integrations/twilio_sms). Они
документированы как legacy YAML notify-платформы; ClickSend конфигурирует
получателя в YAML, а не передаёт произвольный recipient в каждом современном
notify entity action. Они показывают привычный путь уведомлений HA, но не дают
I-Digital Direct-полей и статуса доставки для этого issue.

## Решение

Для штатного интерфейса выбран native `NotifyEntity`: Config Flow хранит один
фиксированный recipient в `ConfigEntry.data`, а `notify.send_message` принимает
только текст и целевую entity. Произвольного номера в каждом вызове нет.
Выделенный `idigital_sms.get_status` отвечает только за явный запрос статуса.

REST recipe остаётся вариантом для ручной отправки и автоматизации через
`rest_command`, но не заменяет этот config flow и notify entity. Отдельное
действие `idigital_sms.send_sms` с динамическим получателем в текущем кандидате
не реализовано. Чатные confirmations и единый assistant dispatcher остаются
отдельной работой и не считаются частью этой интеграции.

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
  границу повторного использования. Текущий узкий клиент находится в
  `distribution/src/idigital_sms/api.py`: он отправляет один Direct object в
  массиве и сохраняет Basic token без повторного Base64-кодирования. История
  SMSforCRM и CRM storage/config в Home Assistant не импортируются.

## Настройка владельцем

Путь: Home Assistant → **Settings → Devices & services → Add integration →
I-Digital SMS**. Форма принимает API key, sender name и один номер E.164.
Ключ отображается password selector и сохраняется в штатной записи HA; это не
шифрование резервной копии. Проверка формата происходит локально: flow не
обращается к Direct и не отправляет тестовое SMS. Синтаксически корректная
запись не доказывает, что ключ, sender или номер разрешены аккаунтом.

И-Digital использует Basic auth с уже Base64-ключом `key_id:password`; повторно
кодировать его нельзя. Вызов выполняется только с сервера: документация
провайдера прямо запрещает использовать Direct API из браузера. Не проксировать
ключ через фронтенд или Habab public demo.

## Сервис и статусы

Для отправки выберите созданную SMS notify entity в действии
`notify.send_message`, укажите `message`. Entity ID назначает Home Assistant;
выберите его из списка после настройки.

У entity всегда один recipient из конфигурации; его нельзя переопределить
параметром вызова. Перед HTTP локально проверяются непустой текст и предел
одной тарифицируемой части (160 GSM-7 units или 70 UTF-16 units). Payload
содержит `channelType=SMS`, сохранённые `senderName` и recipient без `+`, и
текст. Один вызов делает ровно один POST без callback URL. Ответ `errors=false`
с одним UUID означает принятие провайдером, но не доставку.

После отправки UUID доступен как атрибут entity `message_uuid`. Отдельный вызов
`idigital_sms.get_status` получает `message_uuid` и делает один GET; он
возвращает безопасную классификацию:

- `accepted` — запрос создан, конечная доставка ещё не подтверждена;
- `delivered` — Direct сообщил `delivered`;
- `error` — Direct сообщил финальный неуспешный статус;
- `unknown` — ответ не удалось надёжно сопоставить с известным состоянием.

Официальный GET `/message/{message_uuid}` принимает также `externalMessageId`;
документация предупреждает, что в первые 10 секунд после отправки может прийти
404. Кандидат возвращает `unknown/not_found`, а не ошибку доставки. Проверку
статуса запускает пользователь или automation; постоянного фонового опроса нет.
Непонятный статус Direct отображается как `unknown`.

Источники: [авторизация и типы ключей](https://api.docs.direct.i-dgtl.ru/authorization/),
[POST SMS и `errors=false` / `messageUuid`](https://api.docs.direct.i-dgtl.ru/messages/sms-sending/),
[GET сообщения по UUID](https://api.docs.direct.i-dgtl.ru/messages/get/).

## Чаты и подтверждение

**Не реализовано в текущем кандидате.** Эта часть ниже остаётся следующим
контрактом для возможной отправки из web/Telegram чатов и native Assist.

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

- Текущий кандидат фиксирует ровно один recipient на ConfigEntry, принимает
  один текст за вызов и отклоняет сообщения длиннее одной тарифицируемой части.
  Отдельные повторные вызовы не объединяются и не ограничиваются по частоте.
- Будущие локальные лимиты (например 1 SMS в минуту и 10 в час на ConfigEntry)
  должны быть подтверждены до реализации; это не лимиты I-Digital.
- Дедупликация вызовов и защищённый request ID пока не реализованы. Нельзя
  утверждать, что два отдельных одинаковых вызова безопасно объединятся.
- Не делать retry POST `/message` после timeout, сброса соединения или
  невалидного ответа: провайдер мог принять сообщение. Кандидат возвращает
  безопасный `unknown_outcome` и не повторяет запрос автоматически. Новый
  пользовательский вызов всё ещё может отправить SMS повторно.
- Код пишет в лог только безопасный код ошибки; не пишет key, номер, текст или
  полный provider body. Callback URL не задаётся. Для DLR доступен только
  явный status GET.

## Тесты и границы приёмки

**Выполнено для source candidate:** 7 офлайн-протокольных тестов проверяют
payload из одного Direct object, фиксированный recipient, Basic auth,
отсутствие POST на невалидных данных, mapping отказа, отсутствие retry на
неоднозначном исходе и явный status GET. В pinned Core image `fc6062…` smoke
проверил загрузку Config Flow, password selector и штатный `NotifyEntity` handler
с fake client. Существующий suite `test_setup_actions.py` (22 теста) прошёл на
том же Core image с четырьмя компонентами mounted read-only; catalog expectation
обновлён для нового bundled flow.

Эти локальные проверки не сохраняют ConfigEntry в полноценном работающем HA,
не делают запросы к I-Digital и не доказывают отправку, receipt или получение
SMS. Также не проверены chat confirmation, rate limits и deduplication.

**Provider E2E — отдельное разрешение перед выполнением:** сначала владелец
проверяет в личном кабинете тип ключа, остаток бонусного баланса, sender и
зарегистрированный номер; настройки провайдера не менять. Один ранее
разрешённый SMS не является доказательством этой HA-интеграции. Новую отправку
в рамках этой реализации не выполняли. Для любого следующего provider E2E
использовать только отдельно подтверждённый один номер и точный текст, затем
проверить `messageUuid` и provider status. После проверки запросить подтверждение
получения у владельца.

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

## Точки кода для следующего среза

Текущий native candidate находится в `habab/distribution/src/idigital_sms/`;
`Dockerfile` и `habab_home/setup_actions.py` подключают компонент в образ и
каталог config flows. Дальнейшая работа — только после отдельной проверки
владельцем scope: единый gate и подтверждение для web/Telegram/native Assist,
затем согласованные локальные rate limit и deduplication. Не объявлять эти
возможности готовыми по факту добавления notify entity. Live HA ConfigEntry и
provider delivery receipt остаются отдельными проверками. Shared SMSforCRM
package, public release и изменения live provider account не являются частью
текущего кандидата.
