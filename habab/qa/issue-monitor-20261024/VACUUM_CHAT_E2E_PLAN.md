# Проверка управления пылесосом через чат

**Статус: подготовлено, не запускалось.** Владелец подтвердил одно существующее vacuum-устройство в состоянии `idle` с `supported_features=12312`. Runner не создаёт, не удаляет и не переподключает устройство; до отдельного GO от root он не открывает browser и не отправляет команды.

## Проверяемая последовательность

Один проход в текущем административном чате: `start` → `stop` → `return_to_base`. Каждый шаг отправляется видимым полем чата клавишей Enter. Runner требует тот же origin, документ, chat element, пользователя-администратора и session ID на всех трёх шагах. `/owner-test` и `/home/overview` открываются последовательно в одном owned Chromium с ожиданием `networkidle2`. Затем до чтения цели и первой команды runner не менее 8 секунд проверяет, что регистрация и активный service worker controller находятся в состоянии `activated`, а controller не менялся; общий предел ожидания — 120 секунд. Если условие не выполнено, сценарий завершается до любой команды. Это изолирует цикл от незавершённой смены controller, но не доказывает исправление причины прежней перезагрузки.

До первой команды runner заново читает HA states, entity registry и device registry из текущего authenticated UI. Он продолжает только при одной доступной сущности `vacuum.*`, единственной соответствующей entity/device registry записи, начальном состоянии `idle` или `docked` и поддержке `START`, `STOP`, `RETURN_HOME`. Native и REST чтения должны давать одно и то же начальное состояние. Entity ID берётся только из этого readback и остаётся приватным. При нескольких пылесосах, недоступном устройстве, состоянии вне `idle`/`docked` или несовпадении registry/state runner прекращает сценарий без команды.

Каждый Enter остаётся видимым вводом в поле чата. До нажатия runner устанавливает узкий перехват `chat.send`; он подтверждает один `send_started`, один оптимистически принятый `send_accepted` и завершённый `send_completed` на том же document/chat/session с непустым ответом и idle без ошибки. Текущий user prompt должен быть последним user сообщением; ответом считаются непустые assistant сообщения после него. Проверка работает, когда один turn содержит несколько assistant сообщений, и при замене или обрезке истории до 60 записей. Отдельное native HA WebSocket-соединение проверяет `auth/current_user` против пользователя браузера и непрерывно слушает `call_service` через общий `NativeHaServiceObserver` из `service-call-e2e.cjs`. В каждом окне действия принимается ровно одно событие с ожидаемыми `domain=vacuum`, service (`start`, `stop`, `return_to_base`), единственной точной entity-целью и тем же `context.user_id`. В Core event target может приходить через `service_data.entity_id`, оставляя `target` пустым и `target_fields` пустым; runner проверяет нормализованный список `entity_ids` и отвергает дополнительные device/area/floor/label targets. Параллельно runner независимо опрашивает REST state именно этой сущности.

Ожидаемые состояния: сначала `cleaning`, после остановки `idle` или `paused`, после отправки на базу — `docked`. Тайм-ауты ограничены 90 секундами для старта, 60 секундами для остановки и 4 минутами для прибытия на базу. Сервисные feature-биты взяты из pinned Home Assistant Core: `STOP=8`, `RETURN_HOME=16`, `START=8192`; имена сервисов — из `vacuum/services.yaml` для Core 2026.10.2 ([enum](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/vacuum/const.py), [схема сервисов](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/vacuum/services.yaml)). Для каждого следующего действия состояние должно входить в ожидаемый предыдущий набор; второй цикл не запускается.

## Аварийная остановка

Аварийное восстановление возможно только после одного точного `vacuum.start` события этой попытки: оно должно прийти после `send_accepted` этого же видимого chat turn, с точной entity-целью и тем же `context.user_id`. Одного REST состояния `cleaning`/`returning` недостаточно; если движение есть, но owned start не доказан, прямых сервисов нет и требуется ручная проверка. Оно отмечается завершённым только при успешном native stop и его точном service event, свежем `idle`/`paused`, успешном native return, его точном service event и финальном `docked`. Внешний переход в `docked` не заменяет подтверждение вызова и события; иначе восстановление неполное и требует ручной проверки.

Перед остановкой runner подтверждает admin-пользователя native observer, mapping исходной web session через `habab_home/agent/diagnostics`, завершает только этот активный turn (видимой кнопкой при неизменившемся DOM либо ограниченным `agent/abort` только для точно mapped session), затем перечитывает states и entity/device registry. Перед каждым прямым сервисом он сравнивает неизменность `entity_id`, `entity_registry_id`, `device_id`, `config_entry_id`, platform и supported features, а также свежие HA/REST states. Прямой `vacuum.return_to_base` разрешён лишь после успешного native `vacuum.stop`, его точного service event с owner context и свежего состояния `idle`/`paused`; при ошибке или несовпадении event остановка не подтверждена и dock пропускается. Любая аварийная команда оставляет общий verdict `FAIL`, даже если восстановление дошло до `docked`. Если owner, mapping или device binding нельзя повторно доказать, команды не выполняются и отчёт требует ручного вмешательства.

## Доказательства и границы

Публичный JSON содержит release identity, этапы, счётчики и булевы результаты без entity/device ID и raw сообщений. Private trace, режим `0600`, хранит точные registry IDs, session ID, prompts/ответы, native service events с target/context и временную шкалу REST states. Пути отчёта и trace обязательны, должны быть новыми и проверяются до браузерной части; прежние результаты не перезаписываются. Browser создаётся через `withOwnedChromium` и закрывается в `finally`; runner не трогает чужие Chrome, containers, HA-конфигурацию или другие устройства.

Автоматический успех выдаётся как `PASS_PENDING_MANUAL_REVIEW`, если все три prompt/reply, service events, контекст и ожидаемые states подтверждены. Root проверяет приватные ответы помощника перед финальным PASS. Отчёт всегда ставит `physical_motion_observed=false`: состояние HA и вызов сервиса не доказывают, что человек видел движение робота. При любом сбое после фактического старта аварийный fallback не превращает сценарий в PASS.

## Условия перед отдельным GO

- Root подтверждает отдельное эксклюзивное execution window и отсутствие другого live-чата или управляющего клиента.
- Root обновляет приватный auth-файл и собирает свежий runtime identity файл для точно работающего owner-test кандидата. Image, source, runtime, Habab и Core версии передаются runner как строгие ожидаемые значения.
- `--native-ws-url` указывает на loopback WebSocket именно этого HA Core (`ws://127.0.0.1:<port>/api/websocket`). Не использовать внешний или общий Chrome.
- Отчёт и private trace выбираются с новым номером попытки. Не повторять запуск с уже существующими путями.
- Не запускать, если единственная vacuum-сущность не в состоянии `idle` или `docked`, если `START`, `STOP` или `RETURN_HOME` не поддерживаются, либо если native observer не авторизовался тем же admin-пользователем.

## Команда после отдельного GO от root

Команда является шаблоном: root подставляет актуальные значения release identity, порт Core, новый execution-window ID и новые пути. До этого её не запускать.

```sh
node habab/qa/issue-monitor-20261024/vacuum-chat-e2e.cjs \
  --run-live \
  --auth-file /root/.local/share/habab-owner-test/qa-current-auth.json \
  --runtime-identity "$HABAB_QA_RUNTIME_IDENTITY" \
  --expected-image "$HABAB_QA_EXPECTED_IMAGE" \
  --expected-source "$HABAB_QA_EXPECTED_SOURCE" \
  --expected-runtime "$HABAB_QA_EXPECTED_RUNTIME" \
  --expected-product-version "$HABAB_QA_EXPECTED_PRODUCT_VERSION" \
  --expected-core-version 2026.10.2 \
  --native-ws-url "$HABAB_QA_NATIVE_WS_URL" \
  --execution-window-id "$HABAB_QA_EXECUTION_WINDOW_ID" \
  --report habab/qa/issue-monitor-20261024/vacuum-chat-e2e-20261025-attempt-N.json \
  --private-trace /root/.local/share/habab-owner-test/vacuum-chat-e2e-20261025-attempt-N.private.json
```

Подготовка проверяется только локально через `node --check`. Отсутствие `--run-live`, GO и уникальных путей исключает случайное выполнение; подготовленный runner в этой задаче не запускался.
