# Опциональные сетевые инструменты Android runtime

Сборка по умолчанию остаётся без браузера и Pyrogram-совместимой библиотеки.
Чтобы включить их, используйте чистый checkout на том полном commit SHA, который
пойдёт в выпуск. Проверка до любых записей требует, чтобы `HEAD` совпадал с
`--source-revision`, а `habab/android/` и `habab/distribution/` не содержали
изменённых или неотслеживаемых входных файлов.

```sh
python3 habab/android/tools/package_runtime.py \
  --signing-key "$HOME/.local/share/habab-android/release-signing.pem" \
  --source-revision "$(git rev-parse HEAD)" \
  --public-base https://runtime.example.invalid/downloads/android \
  --network-tools
```

Замените example.invalid на HTTPS-адрес каталога выпуска и передавайте
существующий ключ подписи runtime.
Полный SHA записывается в подписанный manifest как `sourceRevision` вместе с
`networkTools`. Не подставляйте SHA dirty checkout или выбранный вручную SHA,
который не совпадает с `HEAD`.

## Что войдёт в runtime

- Реальный Alpine Chromium `155.0.8059.84-r0` для `aarch64`, проверенный по
  зафиксированным Alpine 3.24 main/community APKINDEX и hash APK control record.
  Установка выполняется штатным `apk --root` в существующую базу пакетов
  кандидата, а не наложением файлов из APK tar.
- Kurigram `2.2.26`, импортируемый как `pyrogram`, в отдельном каталоге Python
  3.14. Python `pyaes` и `python-socks` должны совпадать с точными версиями
  Core; wheel проверяется по SHA-256 из
  `tools/network_tools/network-tools.lock.json`.
- `/usr/local/bin/habab-browser` запускает установленный Chromium в headless
  режиме с временным профилем. `/usr/local/bin/habab-python-network` запускает
  системный Python с изолированным `PYTHONPATH` библиотеки.

Проверенная базовая среда: Home Assistant Core `2026.10.2`, Alpine `3.24.1`,
`aarch64`, Python `3.14.6`. В исходном runtime Chromium и Kurigram отсутствовали;
`--network-tools` добавляет их только по явному запросу.

## Ограничения проверки

Для чужой ARM64 rootfs на x86_64 без QEMU APK transaction использует
`--no-scripts`. Pin, APK подписи, DB и бинарный Chromium проверяются, но эффекты
post-install scripts и hooks остаются `NOT_RUN`. Сборка не доказывает запуск
Chromium, работу сайтов в PRoot или физическую приёмку на телефоне. Для этого
нужна отдельная проверка целевого Android устройства.

## Повторное использование сохранённой APK-транзакции

Проверенный кандидат issue 21 сохранён вне checkout:

- rootfs: `/root/.cache/habab-android/chromium-apk-transaction-proof-20261025/rootfs`
- runtime cache: `/root/.cache/habab-android/chromium-apk-transaction-proof-20261025/cache`
- APK closure и индексы: `cache/network-tools/apk-cache/`
- rootfs занимает `3,627,524,096` байт, runtime cache — `265,400,320` байт;
  весь WIP вместе с proof занимает около `3.9` ГБ. APK closure — `85` архивов,
  `261,778,268` байт.

Чтобы использовать эти данные в будущем frozen checkout без копирования
rootfs/APK и без повторного APK fetch, сначала убедитесь, что его
`habab/android/build/runtime` свободен и что checkout удовлетворяет проверке
источника. Затем cache можно переназначить двумя ссылками:

```sh
ln -s /root/.cache/habab-android/chromium-apk-transaction-proof-20261025/cache \
  habab/android/build/runtime
ln -s ../rootfs \
  /root/.cache/habab-android/chromium-apk-transaction-proof-20261025/cache/rootfs
```

Оба target path должны отсутствовать; не заменяйте существующий build cache.

`package_runtime.py` использует `build/runtime/rootfs`, а network-tools installer
ищет транзакцию по `build/runtime/network-tools/apk-cache/transaction.json`.
Перед повторным использованием он сверяет lock pins, APKINDEX hashes, APK DB,
дельту версий и Chromium executable; совпавшая транзакция пропускает helper и
не обновляет APK cache. Если receipt или candidate не совпадают, упаковщик
завершится с ошибкой, не повторяя установку.

Это сохраняет только APK transaction. Полная сборка всё ещё может потребовать
обычные OpenCode, Node и native frontend artifacts в runtime cache. Создание
ссылок не запускает сборку и не заменяет сборочную/физическую приёмку.
