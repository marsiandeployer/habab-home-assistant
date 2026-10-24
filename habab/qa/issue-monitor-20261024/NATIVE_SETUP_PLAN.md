# Native Home Assistant setup через чат

Статус: **Attempt 3 — PASS** на source `9d795359c22cf2844884caa5affc94209a8120ca`, image `e08f1857…`, Habab 0.3.5 / Core 2026.10.2. Runner SHA-256: `6063f09be2e9745c437cb61d6ae637d68c61911749e5cb529407ee4443567aea`.

Через чат открыта защищённая нативная форма Local To-do; после видимой отправки HA подтвердил одну загруженную запись и одну пустую сущность со state `0`. Ответ помощника в той же сессии назвал точный тестовый список, одну сущность и пустое состояние. Root отдельно проверил сохранённые реальные tool-вызовы и точную очистку: исходные записи не изменены, созданные entry/registry/state/storage удалены, активных Local To-do flows нет, backend idle.

Attempt 1 остаётся FAIL: принятие запроса backend не доказано. Attempt 2 остаётся FAIL до pointer dispatch. Оба отчёта сохранены. Успешный прогон после выравнивания навигации и ожидания готовности не доказывает точную причину прежнего временного сбоя. Проверена одна интеграция; options flow, чужие сессии, голос и физические устройства этой проверкой не покрыты.

## Цель

В установленном Habab Home 0.3.5 попросить встроенный русский чат создать ровно одну тестовую интеграцию Local To-do. Заполнить существующую защищённую нативную форму Home Assistant прямо в карточке этого же чата, независимо проверить загруженную запись и пустую сущность, сделать второй read-only запрос тому же помощнику и удалить только созданную тестом запись Local To-do.

Сценарий не начинает config flow напрямую: единственный способ открыть его — ответ реального чата на пользовательскую просьбу. Runner не вызывает `habab_home/setup/start` или `habab_home/setup/submit`; отправка шага формы идёт через видимый `step-flow-form` и кнопку карточки.

## Release и target guards

Перед браузером runner сверяет с явно заданными ожидаемыми значениями:

- running container image ID и OCI labels `revision`, product version, Core version;
- локальный `habab_home/manifest.json` version;
- после авторизации — Home Assistant Core version и admin-контекст;
- ровно одно выбранное подключение assistant provider;
- точное совпадение origin auth client ID и target URL;
- выданный root ID отдельного execution window (это операторская attestation, не lease/lock);
- отсутствие случайно сгенерированного slug в `.storage/local_todo.*.ics`, config entries и активных Local To-do flows.

Для этой подготовленной команды ожидаются image `sha256:e08f185758287f824bb46eb9c955f2605b10648807939189da663dcbd0dbdd29`, source label `9d795359c22cf2844884caa5affc94209a8120ca`, Habab `0.3.5` и Core `2026.10.2`. Несовпадение любого guard прерывает прогон до запроса чату. Runner использует access token из приватного auth-файла, который root обновляет перед GO; он не вызывает `/auth/token` и не отправляет refresh credential через route. Ошибка HA auth остановит прогон на connection gate.

## Ограниченный сценарий

1. Root обновляет access token в закрытом auth-файле непосредственно перед GO. Runner использует этот token без refresh-запроса и в том же отдельном owned Chromium профиле последовательно открывает `/owner-test`, затем `/home/overview`; обе навигации ждут `networkidle2`, сохраняя origin-cookie. В итоговый sanitized report записывается фактический `location.pathname`. Не использовать общий Chrome/CDP профиль. Эта последовательность совпадает с независимыми inert-runner проверками; сама по себе она не доказывает причину предыдущего readiness failure.
2. Дождаться idle того же административного чата и снять read-only baseline config entries. Сгенерировать случайное имя `habab_qa_local_todo_<timestamp>_<nonce>`; значение не добавлять в первое сообщение модели.
3. Отправить в чат одну просьбу подключить ровно `Local To-do` (`local_todo`) и открыть штатную защищённую форму. Не вызывать flow API вручную.
4. Перед нажатием runner проверяет прямоугольник кнопки и реальный composed hit-test через вложенные shadow roots. Если hit-test ещё не подтверждает кнопку, проверка может повторяться не более 5 секунд только пока совпадают тот же chat node, session ID, textarea/send nodes и точный draft; замена любого из них даёт readiness failure с безопасным snapshot. При успехе runner фиксирует pointerdown/pointerup/click от настоящего Puppeteer click; DOM-click fallback отсутствует. После отправки требуется не позднее 5 секунд увидеть новую optimistic user-message точного prompt и busy-состояние либо assistant-ответ после этой новой записи. Без подтверждения prompt flow немедленно прекращается до ожидания формы.
5. Дождаться одной формы `kind=config`, `step-flow-form` внутри `habab-native-form-context`. Проверить обязательное поле `todo_list_name`, ввести уникальное имя в видимое поле и нажать видимую кнопку формы.
6. Дождаться `done` / `loaded`; независимо перечитать config entries, entity registry и states. Требовать одну новую запись `local_todo`, одну принадлежащую ей `todo` сущность со state `0`, отсутствие иных новых config entries.
7. Отправить один read-only запрос в тот же чат и проверить idle/ответ в том же `sessionId`. Условие завершения опирается на соответствие последней user-команды и более позднего assistant-ответа, а не на рост длины истории, которая ограничена 60 сообщениями.
8. Удалить только точный новый entry ID, если совпали `domain=local_todo`, уникальное имя, отсутствие ID в baseline и приватно прочитанный `storage_key`. Перед удалением runner читает только одну точную запись из `.storage/core.config_entries`, выбранную по domain + entry ID + title, удерживает в памяти только её ID/title/key и сверяет key с ожидаемым slug. Затем вызывает штатный `hass.callApi('DELETE', 'config/config_entries/entry/<id>')`; этот REST endpoint зарегистрирован Core как `ConfigManagerEntryResourceView.delete()`. Проверить отсутствие точной config entry, заранее записанных entity IDs в registry/states и `.storage/local_todo.<key>.ics`.

Путь файла основан на shipped Core `2026.10.2`: `homeassistant/components/local_todo/config_flow.py`, `LocalTodoConfigFlow.async_step_user()` сохраняет `CONF_STORAGE_KEY = slugify(todo_list_name)`; `homeassistant/components/local_todo/__init__.py`, `STORAGE_PATH` — `.storage/local_todo.{key}.ics`, а `async_remove_entry()` удаляет файл по тому же slug. `ConfigEntry.as_json_fragment` не включает `data`, поэтому runner получает storage key только из приватного storage-файла и не ожидает его в `config_entries/get`. В `homeassistant/config_entries.py` `SAVE_DELAY = 1`; приватное чтение повторяется ограниченно до 2,5 секунд, чтобы дождаться штатного отложенного сохранения.

При сбое до создания entry runner отменяет только собственный активный Local To-do flow через видимую кнопку карточки. Если exact target нельзя однозначно подтвердить, runner не удаляет данные и оставляет `cleanup.complete=false` для ручного разбора.

## Секреты и доказательства

- API/refresh tokens, ключи провайдера, сообщения целиком, field values, error stacks и raw WebSocket frames не логируются и не попадают в отчёт.
- До первого prompt private trace с режимом `0600` сохраняет только exact session ID, synthetic fixture name/key, baseline entry IDs и enum-состояние принятия отправки; interaction/entry/entity IDs дописываются сразу при обнаружении. Existing report/private trace paths fail-closed, никогда не перезаписываются.
- Ошибка содержит безопасное `source_error_name` и enum-код, для timeout — с именем стадии; text/stack ошибки не сохраняются.
- Sanitized report записывается в указанный `HABAB_QA_REPORT` с режимом `0600`. Минимальный private trace записывается только под `/root/.local/share/habab-owner-test/` с режимом `0600`; он содержит безопасные этапы, enum-коды ошибки и счётчики page/request/http errors. Cleanup обработчик работает внутри callback owned Chromium; перед отменой собственного Local To-do flow он ограниченно останавливает свой активный chat turn.
- Скриншоты не создаются.
- Root должен остановить конкурирующий live E2E и выдать свежий отдельный execution window ID. Его используют только для этого прогона; ID документирует операторское окно, но runner не реализует distributed lease.
- Runner требует `HABAB_QA_GO=1`, `HABAB_QA_EXECUTION_WINDOW_ID`, loopback или утверждённый HTTPS origin, точные release параметры и текущий Docker container. Перед Stop и каждым мутирующим cleanup действием runner сверяет текущую сессию чата и точное последнее отправленное им user-сообщение. При неизвестном/изменившемся владельце ничего не останавливает, не удаляет и сообщает `cleanup.complete=false`.

## Команда после отдельного GO от root

```bash
HABAB_QA_GO=1 \
HABAB_QA_BASE_URL=https://habab-demo.wpmix.net \
HABAB_QA_CONTAINER=habab-owner-test \
HABAB_QA_EXPECTED_IMAGE_ID=sha256:e08f185758287f824bb46eb9c955f2605b10648807939189da663dcbd0dbdd29 \
HABAB_QA_EXPECTED_SOURCE=9d795359c22cf2844884caa5affc94209a8120ca \
HABAB_QA_EXPECTED_PRODUCT=0.3.5 \
HABAB_QA_EXPECTED_CORE=2026.10.2 \
HABAB_QA_EXECUTION_WINDOW_ID=replace-with-root-issued-window-id \
HABAB_QA_AUTH_FILE=/root/.local/share/habab-owner-test/qa-current-auth.json \
HABAB_QA_REPORT=/root/homeassistent/habab/qa/issue-monitor-20261024/native-setup-e2e-attempt-3.json \
HABAB_QA_PRIVATE_TRACE=/root/.local/share/habab-owner-test/native-setup-e2e-attempt-3.private-trace.json \
node habab/qa/issue-monitor-20261024/native-setup-e2e.cjs
```

Принятый Attempt 3 завершился PASS; отчёт и private trace сохранены по указанным путям. Дополнительная независимая проверка отражена в `native-setup-attempt3-independent-verification.json`. Эти output paths существуют и runner откажется перезаписывать их; следующий live-прогон требует отдельного root GO и новых output paths.
