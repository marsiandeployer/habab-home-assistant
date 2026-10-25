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

Владелец задаёт один обязательный фиксированный номер при настройке. Стандартное
действие `notify.send_message` требует текст и целевую SMS entity; entity
указывает на сохранённого получателя, отдельного номера в каждом вызове нет.
Автоматизации и чатный service dispatcher могут вызвать этот стандартный путь.
`idigital_sms.get_status` выполняет отдельный GET по UUID, который возвращается
после ответа Direct. Штатный notify state означает время
последнего принятого вызова, а UUID доступен атрибутом entity; ни то, ни другое
не доказывает доставку.

Кандидат не проверяет ключ запросом к провайдеру и не отправляет тестовое SMS.
Общий чатный `call_service` может вызвать `notify.send_message` с выбранной SMS
entity и обязательным текстом. Timeout даёт неопределённый исход и автоматически
не повторяется. SMS delivery и получение владельцем не проверены.

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

Для штатного интерфейса выбран native `NotifyEntity`: Config Flow обязательно
хранит один фиксированный recipient в `ConfigEntry.data`, а
`notify.send_message` требует текст и target с выбранной SMS entity.
Произвольного номера в каждом вызове нет. Выделенный
`idigital_sms.get_status` отвечает только за явный запрос статуса.

REST recipe остаётся вариантом для ручной отправки и автоматизации через
`rest_command`, но не заменяет этот config flow и notify entity. Отдельное
действие `idigital_sms.send_sms` с динамическим получателем в текущем кандидате
не реализовано.

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
  Generic `call_service` в dispatcher позволяет вызывать стандартный
  `notify.send_message`; он передаёт сервисный вызов HA в контексте owner.
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

- `accepted` — Direct сообщил `sent`; это не подтверждение доставки;
- `delivered` — Direct сообщил `delivered`;
- `error` — Direct сообщил `undelivered`;
- `unknown` — 404 либо состояние, которому клиент не приписывает семантику.

В ответе дополнительно возвращается `provider_status` как безопасная
нормализованная строка статуса. Классификация опирается только на состояния,
подтверждённые опубликованными ответами/событиями Direct: `sent`, `delivered`,
`undelivered`. Значения вроде `queued`, `sending`, `expired`, `unsent` и
`stopped` остаются `unknown`, пока их значение не подтверждено документацией.

Официальный GET `/message/{message_uuid}` принимает также `externalMessageId`;
документация предупреждает, что в первые 10 секунд после отправки может прийти
404. Кандидат возвращает `unknown/not_found`, а не ошибку доставки. Проверку
статуса запускает пользователь или automation; постоянного фонового опроса нет.
Непонятный статус Direct отображается как `unknown`.

Источники: [авторизация и типы ключей](https://api.docs.direct.i-dgtl.ru/authorization/),
[POST SMS и `errors=false` / `messageUuid`](https://api.docs.direct.i-dgtl.ru/messages/sms-sending/),
[GET сообщения по UUID](https://api.docs.direct.i-dgtl.ru/messages/get/).

## Доступность из встроенного чата

Встроенный web/Telegram assistant использует общий `setup_actions.execute`
dispatcher с generic `call_service`. Он может вызвать штатный
`notify.send_message`, передав выбранную SMS entity как target и текст как
обязательное поле `message`. Получатель зафиксирован в Config Flow и связан с
entity; HA выполняет service call в контексте активного owner. Этого достаточно
для встроенного чатного пути. Отдельный SMS service, LLM tool или confirmation
flow не добавляются.

Native HA Assist использует другой execution path и этим утверждением не
покрывается.

## Ограничения отправки и повторов

- Кандидат фиксирует ровно одного обязательного recipient на ConfigEntry,
  требует непустой текст и отклоняет сообщения длиннее одной тарифицируемой
  части.
- Локальный per-entry admission limit: максимум одна попытка в минуту и десять
  за скользящий час. Одинаковый текст на том же получателе не допускается
  повторно в течение пяти минут. Проверка и хэши существуют только в памяти;
  рестарт HA сбрасывает эти ограничения. Это защита от частых/повторных вызовов,
  а не durable idempotency и не лимиты провайдера.
- Резервирование происходит до POST, поэтому неясный исход также занимает
  лимит и не повторяется автоматически. После пяти минут отдельный явный вызов
  всё ещё может отправить то же сообщение.
- Не делать retry POST `/message` после timeout, сброса соединения или
  невалидного ответа: провайдер мог принять сообщение. Кандидат возвращает
  безопасный `unknown_outcome` и не повторяет запрос автоматически. Новый
  пользовательский вызов всё ещё может отправить SMS повторно.
- Код пишет в лог только безопасный код ошибки; не пишет key, номер, текст или
  полный provider body. Callback URL не задаётся. Для DLR доступен только
  явный status GET.

## Тесты и границы приёмки

**Выполнено для source candidate:** офлайн-протокольные тесты проверяют
payload из одного Direct object, фиксированный recipient, Basic auth,
отсутствие POST на невалидных данных, отсутствие retry на неоднозначном исходе,
явный status GET, только подтверждённое отображение статусов и ограничения
частоты/повторов. В pinned Core image `fc6062…` smoke
проверил загрузку Config Flow, password selector и штатный `NotifyEntity` handler
с fake client. Существующий suite `test_setup_actions.py` (22 теста) прошёл на
том же Core image с четырьмя компонентами mounted read-only; catalog expectation
обновлён для нового bundled flow.

Эти локальные проверки не сохраняют ConfigEntry в полноценном работающем HA,
не делают запросы к I-Digital и не доказывают отправку, receipt или получение
SMS. Отдельный runtime chat E2E, сохранение rate limits после перезапуска и
provider E2E не выполнялись.

**Provider E2E — одно разрешённое SMS, ещё не выполнено:** перед ним проверить
тип ключа, остаток бонусного баланса, sender и зарегистрированный номер в
личном кабинете; настройки провайдера не менять. Предыдущее SMSforCRM не
доказывает работу HA integration. Перед отправкой сверить точный получатель и
текст с разрешённым тестом, затем проверить `messageUuid` и provider status.
После проверки получить от владельца подтверждение получения.

## Preflight одного разрешённого provider test

Перед единственной отправкой оператор проверяет без вывода ключей/текста в
логи или чат:

1. Проверить существующую ConfigEntry `idigital_sms` (`default`) и использовать
   её, если она уже настроена для этого теста. Если записи нет, сначала
   подключить интеграцию через штатную форму с выделенным тестовым ключом,
   отправителем и разрешённым получателем. Не создавать дубликат и не менять
   чужую запись. Без вывода ключа сверить совпадение получателя с тестовым номером.
2. В личном кабинете I-Digital оператор подтверждает тип ключа Direct, что
   аккаунт разрешает тест на этом зарегистрированном номере, актуальный остаток
   бонусного баланса и активное/разрешённое имя отправителя. Если бесплатный
   режим или номер не подтверждены — остановиться, не отправлять.
3. В UI Home Assistant выбрать единственную SMS entity и выполнить один
   `notify.send_message` с заранее показанным владельцу текстом
   `Тест SMS Habab Home: проверка доставки.` Повторно не вызывать при timeout,
   ошибке UI или неизвестном исходе.
4. После ответа Direct прочитать UUID из атрибута `message_uuid` у этой notify
   entity и один раз вызвать `idigital_sms.get_status`. Если ответ
   `unknown/not_found`, подождать не менее
   10 секунд и сделать одну явную повторную проверку статуса; не повторять POST.
5. Сверить `delivered` от Direct, затем получить от владельца подтверждение
   получения. Сохранить только обезличенный результат, статус и UUID; не
   сохранять ключ, номер или содержимое SMS в отчёт.

До проверки пунктов 1–2 и выполнения одного вызова provider E2E имеет статус
`NOT RUN`. Source tests или наличие стандартного notify service этого не меняют.

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
