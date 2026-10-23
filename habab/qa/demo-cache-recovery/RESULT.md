Пользователь сообщил об `Unable to connect` на
https://habab-demo.wpmix.net/?habab-preview=onboarding:
`core.b387881ced09e4aa.js`, `/auth/token` 404 и ошибка `/api/websocket`.

Имя core-бандла совпало с установленным frontend прежнего `habab-owner-test`.
В текущем публичном compiler tree такого бандла нет. Сервер отдаёт HTML с
`data-habab-demo=true` и `main.7fb75074ba88b0cf.js` из релиза `20261021-ui5`.
В чистом браузерном контексте исходная ошибка не воспроизвелась.

Исходники HA показывают, что прежний root service worker мог выдавать сохранённый
HTML `/`, игнорируя query. API и auth при этом идут в сеть. Так публичный адрес
может получить старую оболочку HA, которая затем обращается к отсутствующей
авторизации. Это подтверждённый сценарий отказа; состояние конкретного браузера
пользователя не осматривалось, поэтому его точная причина остаётся неподтверждённой.

На трёх прежних адресах worker опубликован один общий скрипт:
`/sw-modern.js`, `/sw-legacy.js`, `/service_worker.js`. Он активируется,
снимает регистрацию и обновляет только окна публичного корня или
`/demos/home-assistant/`. Query и fragment сохраняются. У скрипта нет fetch handler;
он не читает и не удаляет caches, cookies, localStorage, авторизацию или настройки.
Приватный `/owner-test` и другие маршруты не перенаправляются. Оба publisher
используют одну Python-функцию, размещая worker до переключения index.

Проверено:

- 4 Node-теста: жизненный цикл, границы адресов, ошибки и timeout навигации,
  ограничение числа окон, совпадение трёх публичных копий с исходником.
- 1 Python-тест publisher: одинаковые файлы и повторяемые заголовки.
- Настоящий Chromium, три прежних имени worker: искусственная сохранённая
  оболочка сначала вызывает auth/token и WebSocket, после обновления worker
  открывает текущий compiled demo и чат. После reload нативных auth-запросов нет.
  Cookies, synthetic auth sentinel и оба тестовых cache сохранены. Это локальная
  проверка с fixtures API; реальный дом или данные аккаунтов не использовались.
- Публичные три адреса вернули HTTP 200, JavaScript MIME, `Cache-Control: no-store`
  и одинаковый SHA-256 `2c0cacaed2cbc4f831d267e1741abff431ff54dfe930f755c3cd56e48f6e24fb`.
- Публичный preview проверен в Chromium 1280×800 и 390×844: чат открыт,
  три строки состояния, ввод и отправка видимы; page errors, auth-запросов,
  WebSocket и новых регистраций worker нет. Контексты закрыты, чужие страницы
  сохранены. Локальный QA HTTP server остановлен.
- Независимое ревью кода не выявило блокеров.

Как проверить: обновить preview через Ctrl+F5. Ожидается открытый чат с вопросом
об оборудовании, без `Unable to connect`. Подтверждение в затронутом браузере
пользователя ещё ожидается. Повторение автоматических проверок:

```sh
node --test habab/build/test-retire-demo-service-worker.mjs
python3 habab/build/test_demo_service_worker.py
node habab/qa/demo-cache-recovery/check-recovery.cjs
node habab/qa/demo-cache-recovery/check-live.cjs
```

Новые обращения к модели не выполнялись: prompt и API чата в этой правке
не менялись. Компилятор HA не запускался; приватный Core и Windows не изменялись.
