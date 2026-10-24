# CC2531 по USB-OTG к Android-серверу без root

Исследование для GitHub issue 15. Источники проверены 24 октября 2026 года.
Это исследовательский результат, не изменение приложения и не приёмка железа.
Базовая линия продукта, которую это исследование не меняет: `96551e39e`,
Habab `0.3.5` / `native14`.

## Вывод

**Передать USB CDC ZNP из Android userspace в Home Assistant под PRoot технически можно; самый малый proof of concept — Android Java USB service как loopback TCP server плюс стандартный ZHA serial path `socket://127.0.0.1:<port>`.** Это незащищённый transport для стенда, доступный локальным приложениям. PRoot напрямую не получает USB permission и не видит `UsbDeviceConnection` как `/dev/ttyACM*`; передача байтов остаётся обязанностью Android service.

Поддерживаемый TI ZNP на CC2531 использует USB CDC (виртуальный COM-порт), а поверх CDC — TI ZNP/MT serial framing. Это не произвольный «USB serial» и не сырой Zigbee радио-поток. Для нормальной работы на адаптере должна быть coordinator-совместимая Z-Stack ZNP прошивка. Модель CC2531 сама по себе не гарантирует ни нужную прошивку, ни USB CDC профиль.

**Зарядка телефона одновременно с USB host не подтверждена.** USB-C поддерживает отдельные power/data роли и их PD-переключение, но это не обещание, что конкретный телефон, хаб и зарядное устройство дадут режим «телефон — data host, телефон — power sink». У Huawei P30 Pro официально описана обратная OTG-зарядка: телефон отдаёт питание подключённому устройству. Это обратное направление и оно не доказывает зарядку P30 Pro во время host-режима.

## Какой транспорт нужен

1. Приложение перечисляет USB интерфейсы, проверяет CDC ACM control interface, связанный с ним data interface, и bulk IN/OUT endpoints. Для реального устройства связь интерфейсов следует проверять по CDC Union descriptor; один лишь VID/PID или метка «USB serial» недостаточны.
2. После USB permission Java foreground service вызывает `claimInterface`, настраивает CDC line coding и CDC control-line state, выполняет async bulk IN/OUT и переживает detach/re-attach с повторным запросом разрешения.
3. ZHA получает byte stream через текущий zigpy → serialx transport. В upstream обе библиотеки принимают `socket://`; ZHA config flow позволяет вручную задать этот путь и radio type `znp`. Поэтому не нужен новый zigpy backend для сырых байтов.
4. Уточнение по bootloader: zigpy-znp вызывает DTR=false и RTS false/true/false, но serialx TCP transport эти modem-pin вызовы делает no-op. При default `skip_bootloader: true` код затем, если ping не ответил, отправляет CC253x FORCE_RUN bytes и снова пробует ping. Следовательно **обычный byte bridge может быть достаточен** без передачи DTR/RTS; конкретный CC2531/firmware от этого не становится проверенным. Если стенд покажет, что FORCE_RUN fallback не срабатывает, Android service может сам выполнить нужные CDC control-line transitions при новом socket connection перед передачей первых байтов. Если firmware требует произвольного дальнейшего изменения modem pins, `socket://` не передаёт их; тогда нужен RFC2217 или расширение транспорта.
5. Для стендового proof of concept Android service задаёт CDC line coding (обычно ZNP 115200, 8-N-1), принимает один TCP client и прозрачно пересылает bulk IN/OUT данные, порядок и EOF при USB detach. HA конфигурируется стандартным `socket://127.0.0.1:<port>` через ZHA manual serial path. Production-safe Unix socket потребует узкого serialx/ZHA transport connector; полный новый ZNP backend не нужен.

**Граница безопасности:** `socket://127.0.0.1:<port>` — только прототип/стендовый вариант. Listener не аутентифицирует клиента, а обычный ZHA socket transport не посылает token handshake; short-lived token защиту здесь не добавляет. Loopback блокирует LAN-доступ, но доступен другим приложениям на телефоне. Для продукта безопаснее приватный filesystem Unix socket в каталоге приложения. Проверенный upstream serialx не регистрирует `unix://`/AF_UNIX URL, поэтому ZHA штатно подключиться к такому socket не может. Минимальная product work для этой границы — узкий serialx/ZHA transport connector для Unix domain socket; это не требует писать заново ZNP backend. Пока такого connector нет, localhost TCP остаётся явным security tradeoff, а не token-protected transport.

## Фон и питание

USB Host API зависит от возможностей аппаратуры. В Android host mode телефон — host и подаёт питание на USB шину. Для долгого USB обмена следует использовать foreground service с типом `connectedDevice`, когда он доступен. Android 14 требует `FOREGROUND_SERVICE_CONNECTED_DEVICE` и одного из указанных prerequisites для приложений с target API 34+; USB permission, полученный через `UsbManager.requestPermission()`, удовлетворяет одному условию. Локальная APK сейчас имеет `targetSdk 28`, `compileSdk 34`, поэтому Android 14 требования для target 34+ не следует приписывать текущей сборке. При повышении target SDK их надо добавить и перепроверить. USB permission относится к подключению устройства и после unplug может потребовать повторного запроса.

Для зарядки нужен хаб с внешним питанием/PD pass-through и фактической поддержкой нужной комбинации ролей. Даже наличие USB-C PD у зарядника/хаба не говорит, что Android останется DFP/Host, принимая питание как Sink. Приёмка должна сначала подтвердить работу USB Host без зарядки, затем с хабом отдельно проверить: устройство остаётся перечисленным, Android permission сохраняется, HA не теряет транспорт, статус телефона показывает заряд, а батарея не разряжается под нагрузкой. Модель хаба, его PD вход/выход, зарядник, кабель, прошивка телефона и версия Android — часть результата.

## Матрица фактов и проверки

| Пункт | Известно из источников | Проверено здесь | Физическая проверка |
|---|---|---|---|
| Android USB Host | Android API перечисляет устройства, интерфейсы, endpoints и требует user permission до обмена | Документация Android, 24.10.2026 | **NOT RUN** — телефон/USB не трогались |
| Huawei P30 Pro platform declaration | Модель VOG-L29, Android 10/API 29; `pm list features` показывает `android.hardware.usb.host` | Read-only receipt предоставил parent 24.10.2026; я не подключался к ADB и телефон не менял | **NOT RUN** — CC2531 enumeration, USB permission, CDC transfer и зарядка не проверялись |
| Android lifecycle | `connectedDevice` — подходящий foreground service type; специальное разрешение/prerequisites Android 14 обязательны при target API 34+ | Android docs; локальная APK `targetSdk 28`, `compileSdk 34`, 24.10.2026 | **NOT RUN** — фоновая служба не запускалась |
| CC2531 интерфейс | TI ZNP для CC2531 описывает CDC virtual COM; ZNP использует UART transport packet format поверх USB CDC | TI SWRA442, стр. 22–23; zigpy-znp docs, 24.10.2026 | **NOT RUN** — конкретный донгл, дескрипторы и firmware не проверялись |
| CC2531 ZHA backend | HA ZHA перечисляет CC2530/CC2531 coordinator stick с Z-Stack firmware; CC2531 устарел и не рекомендуется, ограничения сети порядка 15–20 устройств | HA docs и zigpy-znp docs, 24.10.2026 | **NOT RUN** — ZHA с физическим адаптером не запускалась |
| ZHA socket path | ZHA manual path принимает `socket://...`; zigpy передаёт URL в serialx; serialx socket transport не аутентифицирует клиента и no-op-ит modem pins | Pinned upstream Core/zigpy/serialx source, 24.10.2026 | Source inspection **PASS**; Android loopback bridge не запускался |
| CC2531 startup over socket | zigpy-znp default `skip_bootloader:true`; pin toggles отсутствуют в socket transport, но код после неуспешного ping отправляет CC253x FORCE_RUN bytes | Pinned upstream `zigpy-znp` source, 24.10.2026 | **NOT RUN** — реальный fallback и конкретная firmware не проверены |
| Телефон заряжается в Host mode | USB-C различает power и data роли; смена роли возможна через USB PD | USB-IF Type-C spec/system overview, 24.10.2026 | **NOT RUN** — конкретная пара телефон/хаб/зарядник неизвестна |
| Huawei P30 Pro + зарядка | Huawei описывает OTG reverse charging, телефон выступает источником питания | Huawei support page для P30 Pro, 24.10.2026 | **NOT RUN** — P30 Pro в USB host не подключался |
| Android PRoot ↔ HA | Android USB permission относится к приложению с `UsbDeviceConnection`; существующий PRoot запускает Python напрямую, без Docker и root | локальный `habab/android/README.md`; вывод по API | **NOT RUN** — IPC или transport shim не разрабатывался в production |

«Проверено здесь» означает проверку официальной документации/исходников и локальных офлайн-тестов. Это не проверка устройства. «NOT RUN» не следует трактовать как отказ аппаратуры.

## Матрица совместимости

| Комбинация | Оценка |
|---|---|
| CC2531 + заранее установленная совместимая Z-Stack ZNP coordinator firmware | Протокол поддерживается zigpy-znp; старое и ограниченное решение. Нужна проверка конкретного firmware/profile |
| CC2531 с неизвестной, router, bootloader-only или иной firmware | Не считается совместимым; не перепрошивать в рамках этой проверки |
| Android, который аппаратно поддерживает USB Host + USB CDC ACM device | Условно совместим через Java USB Host API при USB permission и loopback `socket://` byte bridge |
| Android/P30 Pro + произвольный OTG splitter | Совместимость и зарядка не подтверждены |
| PD powered hub с pass-through | Возможный кандидат; совместимость только по конкретной модели и тесту на телефоне, наличие PD само по себе недостаточно |
| PRoot напрямую открывает `/dev/ttyACM*` | Не является допустимым предположением: app-level USB permission/connection не создаёт устройству Linux tty |
| ZHA через raw loopback TCP | Для стенда `socket://` путь поддержан; TCP modem pin control no-op, default ZNP startup имеет FORCE_RUN byte fallback. Listener не имеет auth и доступен локальным приложениям. Достаточность для прошивки **NOT RUN** |
| ZHA через Unix filesystem socket | Production-safe кандидат, но upstream serialx не поддерживает AF_UNIX URL; нужен узкий connector. Существующий ZNP backend переписывать не требуется |

## Реальный сценарий последующей проверки

Текущий статус CC2531 этапа — **NOT RUN**. В этой подзадаче я не использовал ADB и не подключал USB, телефон или туннели. Parent отдельно сообщил read-only receipt для телефона: VOG-L29 / Android 10 API 29 / `android.hardware.usb.host`; конфигурация телефона не менялась. CC2531 enumeration, CDC transfers, зарядка и livecloud не проверялись. Прошивка, сброс сети, discovery, pairing и радио-команды не запускались.

1. Записать точную модель/ревизию CC2531, состояние его firmware со стороны поставщика, точную модель телефона, Android/EMUI, хаба, PD зарядника и кабелей. Не подключать неизвестный адаптер к рабочей Zigbee сети.
2. При отключённом заряднике установить приложение и запустить явный экран USB-setup. Подключить CC2531 через обычный OTG переходник, подтвердить системный USB permission и зафиксировать только USB enumeration: устройство, интерфейсы, class/subclass/protocol, endpoints, detach. Не отправлять ZNP/radio commands.
3. Проверить в реализации сопоставление CDC Union, claim control/data interfaces и корректный отказ при неподдерживаемом интерфейсе. Не менять firmware или сетевые данные.
4. Для исследовательского PoC проверить Android USB→`127.0.0.1` bridge и пройти ZHA config flow с `socket://127.0.0.1:<port>`, radio type `znp`, принимая локальное-app exposure только как риск тестового стенда. Для product пути сначала добавить AF_UNIX connector. ZNP startup probe отправляет байты координатору; физического этапа в этой работе не было.
5. Для питания повторить только после успешного host enumeration: подключить зарядник к PD-in хаба, затем хаб к телефону, сохранив USB устройство подключённым. Зафиксировать Android USB host state, факт зарядки и изменение заряда батареи под нагрузкой. Если смена роли disconnect-ит донгл или телефон перестаёт быть host, комбинация не годится.
6. После интеграции оставить систему на сутки с выключенным экраном, проверить auto-reconnect после detach/re-attach, остановку службы и понятное состояние в UI. Затем провести отдельную длинную проверку охлаждения/зарядки. Тест pairing и реального Zigbee дома проводить отдельно с резервной копией и согласованным окном.

## Первичные источники

Все страницы и ссылки ниже сверены 24.10.2026; у динамических GitHub/docs страниц отдельная дата публикации может не показываться.

1. Android Developers, [USB host overview](https://developer.android.com/develop/connectivity/usb/host) — USB Host API, interfaces/endpoints и user permission.
2. Android Developers, [USB host and accessory overview](https://developer.android.com/develop/connectivity/usb) — Android Host питает шину; поддержка Host зависит от аппаратуры.
3. Android Developers, [Data transfer background task options](https://developer.android.com/develop/background-work/background-tasks/data-transfer-options) и [foreground service types](https://developer.android.com/develop/background-work/services/fgs/service-types) — `connectedDevice` для длительного взаимодействия с USB; Android 14 permission prerequisites действуют при target API 34+. APK в текущем локальном Gradle конфиге: `targetSdk=28`, `compileSdk=34`.
4. USB Implementers Forum, [USB Type-C Specification Release 2.0](https://www.usb.org/sites/default/files/USB%20Type-C%20Spec%20R2.0%20-%20August%202019.pdf) и [USB Type-C System Overview](https://www.usb.org/sites/default/files/D1T1-2%20-%20USB%20Type-C%20System%20Overview.pdf) — определения DFP/UFP, Source/Sink, DRD/DRP и ролевого переключения через PD.
5. Texas Instruments, [Z-Stack ZNP Interface Specification, SWRA442](https://e2e.ti.com/cfs-file/__key/communityserver-discussions-components-files/158/3731.Z_2D00_Stack-ZNP-Interface-Specification.pdf), документ 2015 года — раздел 2.3.1.1.5, стр. 22, указывает, что CC2531 USB транспорт экспонирует CDC virtual COM и передаёт ZNP UART transport; раздел 2.1.3, стр. 8 — SOF `0xFE`, длина, команда, data и XOR FCS.
6. Home Assistant, [ZHA integration](https://www.home-assistant.io/integrations/zha/) — CC2530/CC2531 требуют Z-Stack coordinator firmware; устаревшая платформа и лимит порядка 15–20 устройств.
7. zigpy-znp, [`api.py`](https://github.com/zigpy/zigpy-znp/blob/926626c4035b75f943182e7f41ca5f7c97da4ac1/zigpy_znp/api.py), [`config.py`](https://github.com/zigpy/zigpy-znp/blob/926626c4035b75f943182e7f41ca5f7c97da4ac1/zigpy_znp/config.py) и [`uart.py`](https://github.com/zigpy/zigpy-znp/blob/926626c4035b75f943182e7f41ca5f7c97da4ac1/zigpy_znp/uart.py) — pinned commit `926626c4035b75f943182e7f41ca5f7c97da4ac1`; startup pin sequence и FORCE_RUN bytes fallback.
8. zigpy, [`serial.py`](https://github.com/zigpy/zigpy/blob/f9bb752e11acb43dc075c36f173cb8fd104cba74/zigpy/serial.py) — pinned commit `f9bb752e11acb43dc075c36f173cb8fd104cba74`; передача URL в serialx.
9. serialx, [`serial_socket.py`](https://github.com/puddly/serialx/blob/91d6eaad83d3bf53a77f7cfd1f8156b7a6819ca2/serialx/platforms/serial_socket.py) и [migration guide](https://github.com/puddly/serialx/blob/91d6eaad83d3bf53a77f7cfd1f8156b7a6819ca2/docs/how-to/pyserial-migration.md) — pinned commit `91d6eaad83d3bf53a77f7cfd1f8156b7a6819ca2`; socket URL support и no-op modem pin методы.
10. Home Assistant Core, [ZHA config-flow strings](https://github.com/home-assistant/core/blob/d67481af31d89f6c1d5fcb38c4f532d519e5181d/homeassistant/components/zha/strings.json) — pinned commit `d67481af31d89f6c1d5fcb38c4f532d519e5181d`; manual serial path прямо перечисляет `socket://`.
11. zigpy ZHA package, [`pyproject.toml`](https://github.com/zigpy/zha/blob/6decce1ed47b94a53b0747998ac8e6fb4f013ae3/pyproject.toml) — pinned commit `6decce1ed47b94a53b0747998ac8e6fb4f013ae3`; dependency list includes `zigpy-znp==1.1.1` and `zigpy==2.4.0` at this snapshot.
12. serialx, [URI dispatch](https://github.com/puddly/serialx/blob/91d6eaad83d3bf53a77f7cfd1f8156b7a6819ca2/serialx/common.py) and [registered socket/TCP handlers](https://github.com/puddly/serialx/blob/91d6eaad83d3bf53a77f7cfd1f8156b7a6819ca2/serialx/platforms/serial_socket.py) — pinned commit `91d6eaad83d3bf53a77f7cfd1f8156b7a6819ca2`; this tree registers `socket://`, `tcp://`, RFC2217, ESPHome, and platform device handlers, with no Unix-domain URL handler. Unknown URI schemes raise `UnknownUriScheme`.
13. Home Assistant Core, [ZHA manifest](https://github.com/home-assistant/core/blob/d67481af31d89f6c1d5fcb38c4f532d519e5181d/homeassistant/components/zha/manifest.json) — pinned commit `d67481af31d89f6c1d5fcb38c4f532d519e5181d`; Core installs the `zha` package requirements.
14. Huawei Support, [Reverse Charging with an OTG Cable — HUAWEI P30 Pro](https://consumer.huawei.com/uk/support/content/en-gb00762391/) — подтверждает OTG reverse-charge сценарий и его направление.

## Офлайн прототип

`znp_usb.py` — самостоятельная учебная тестовая модель без USB, Android SDK, сторонних зависимостей и радио-операций; это не production transport/parser proposal. Реальный продукт должен переиспользовать zigpy-znp для ZNP framing. Offline tests распознают заданную CDC ACM/data interface model и проверяют фреймы. Тестовые интерфейсы синтетические: настоящие дескрипторы CC2531 не снимались. Их успех не означает USB, firmware, Android, PRoot, ZHA или физический PASS.

Команда из корня репозитория:

```sh
python3 -m unittest discover -s habab/android/research/cc2531-otg -v
```
