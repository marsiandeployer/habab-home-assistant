# Обновление Home Assistant

Это настоящий GitHub fork `home-assistant/frontend` с общей историей Git. `origin` — `marsiandeployer/habab-home-assistant`, `upstream` — `home-assistant/frontend`. Наша ветка — `main`, upstream использует `dev`. Исходный baseline — `92599a88037ef5b7bd822efd7301028d824f7f5b`.

Изменения русского демо внесены отдельным коммитом в upstream-исходники. API, Docker-дистрибутив и независимые формы находятся в `habab/`.

## Frontend Docker-дистрибутива

Docker Core остаётся на 2026.10.2. Полный frontend собирается из Core-совместимого upstream tag `20260930.4` с патчем `habab.9`; это отдельная сборка от публичного демо в `habab/build/`. Wheel версии `20260930.4+habab.9` публикуется по неизменяемому HTTPS URL с SHA-256 в пути. При `docker build` `install-native-frontend.py` сверяет закреплённые версию, имя файла, размер, origin и digest, затем устанавливает wheel без дополнительных пакетов. В том же build находятся API bridge v1 для глобального чата и native `step-flow-form`; context host использует HA `contextMixin` и сохраняет native callbacks, переводы, selectors и валидацию. Сбой загрузки или несовпадение digest останавливают сборку.

При обновлении Docker Core найдите совпадающие Core и frontend upstream версии, перенесите downstream patch на этот frontend, соберите полный wheel и обновите его закреплённые метаданные только после повторной сборки и QA. Самостоятельная замена frontend wheel не обновляет Home Assistant Core или пользовательские данные.

После клонирования добавьте remote, если его ещё нет:

```sh
git remote add upstream https://github.com/home-assistant/frontend.git
```

Из чистого рабочего дерева запустите:

```sh
sh habab/scripts/update-upstream.sh
```

Скрипт скачивает `upstream/dev`, создаёт ветку `update/home-assistant-…` и выполняет обычный merge. Он не перезаписывает наши коммиты и не публикует сборку. При конфликтах Git останавливается: разрешите их осознанно или отмените операцию через `git merge --abort`. Не разрешайте конфликты автоматически через `ours`/`theirs`.

До слияния в `main`:

1. Изучите upstream release notes и diff наших модификаций.
2. Сверьте русский перевод `habab/build/source/ru.json` с новой версией.
3. Соберите текущий checkout: `sh habab/build/build.sh`. Скрипт устанавливает зависимости по lockfile и по умолчанию задаёт релиз `git-<commit>`. Для другого идентификатора используйте `HABAB_DEMO_RELEASE=20261022-r1 sh habab/build/build.sh`. Существующий релиз не перезаписывается. Версии Node/pnpm закреплены для исходного выпуска; новые требования upstream обновляйте отдельным проверенным изменением.
4. Запустите Python-тесты из `habab/`, frontend проверки по корневому `AGENTS.md` и браузерную проверку состояния, Telegram-формы, карты и iframe.
5. Проверьте изоляцию сессий, видимые ошибки, отсутствие токенов в запросах демо-формы и секретов в diff.
6. Проведите review и слейте ветку после QA. Для публичного демо отдельно соберите и опубликуйте новый immutable ID по инструкции в [README Habab](../README.md); не используйте существующее имя публичного релиза для изменившихся чанков.

## Кандидаты и границы выпуска

Для Docker-кандидата откройте Actions → **Habab release candidate** и выберите `target_scope` (`all`, `windows`, `linux` или `android`) и `source_ref`: полный commit SHA либо существующий tag. Ветку передать нельзя; разрешённый commit должен входить в историю `origin/main`. Workflow собирает выбранные артефакты из чистого checkout именно этого commit. Scope `all` также собирает Android, если настроены runtime RSA signing key и APK signing secrets: сначала компилирует свежий native frontend из того же SHA, затем упаковывает подписанный runtime и APK. Вместо RSA key допустим только заранее подписанный runtime, точно совпадающий с wheel из нового frontend job. При отсутствии prerequisites Android явно получает `blocked`. Невалидный или несовпадающий runtime и ошибка Android build/sign приводят к failure без aggregate manifest. Итоговый `release-candidate.json` фиксирует source SHA, состояние каждой платформы и SHA-256/размер каждого файла; Linux-записи также содержат OCI platform и manifest/config digest. Candidate-артефакты хранятся один день — скачайте их вместе с манифестом до истечения срока.

Receipt [workflow run 40254665226](../qa/issue-monitor-20261026/ci-candidate-59291.json) фиксирует для source `59291f7f2a620fb696438ad027e0d0702f6b0de9` Windows, Linux amd64 и Linux arm64 как `built`, Android как `blocked` (job не запускался). Это результат сборки кандидата, а не подтверждение установки или поддержки платформы. Для Android workflow требует подписанный runtime manifest того же source и secrets подписи.

Workflow кандидатов не публикует стабильный образ, не создаёт Habab release notes и не задаёт процедуру отката. Официальные `Release` и `Release Drafter` workflow ограничены репозиторием/владельцем Home Assistant, поэтому не являются релизным процессом этого fork. До отдельной инструкции для стабильного Docker-релиза используйте резервное копирование `/config` из [инструкции установки](../distribution/README.md#резервная-копия-и-восстановление). Там же указано, что откат через версии Home Assistant, меняющие формат базы, может не поддерживаться; автоматический откат кандидата не предусмотрен.

### Ручное продвижение Docker-кандидата и откат

Этот checklist фиксирует решение оператора; он не запускает публикацию. Для каждого выпуска:

1. Скачайте candidate artifacts и `release-candidate.json` до истечения срока хранения. Сверьте полный `source_revision` в манифесте с одобренным Git SHA и receipt запуска. Не продвигайте платформу со статусом `blocked`, `skipped` или без артефакта.
2. Для каждого скачанного файла пересчитайте SHA-256 и размер и сравните с записью в манифесте. Для Linux дополнительно сохраните OCI platform и manifest/config digest; публикуйте и разворачивайте образ по digest, а не по плавающему тегу. Receipt [run 40254665226](../qa/issue-monitor-20261026/ci-candidate-59291.json) показывает, какие платформы были собраны для SHA `59291f7f2a620fb696438ad027e0d0702f6b0de9`; сами хеши сверяются с загруженным `release-candidate.json`.
3. До переключения запишите предыдущий immutable release для каждой платформы: точный image reference с digest для Linux и точный архив с SHA-256 для Windows. Убедитесь, что предыдущие артефакты доступны для восстановления, и создайте отдельную резервную копию `/config` по инструкции выше.
4. Опубликуйте прошедшие проверки артефакты под новым версионированным идентификатором. Сохраните release notes, source SHA, candidate run/receipt, manifest, SHA-256, OCI digests и предыдущие immutable references. После публикации повторно прочитайте опубликованные артефакты и сверьте их digest/checksum с кандидатом до переключения пользователей.
5. После переключения выполните и запишите отдельные проверки запуска для каждой платформы. Статус `built` и совпавший checksum подтверждают сборку и целостность скачанного файла, но не установку, запуск или поддержку платформы.
6. При сбое остановите дальнейшее продвижение, верните прежний exact image digest или Windows-архив и проверьте запуск. Если новая версия уже меняла формат `/config`, файловый откат сам по себе не откатывает данные: восстановите согласованную резервную копию только по процедуре восстановления и зафиксируйте результат.

Для завершённого продвижения сохраните короткий release receipt: новый идентификатор, точный source SHA, ссылки на candidate receipt и manifest, опубликованные checksum/digest, статус проверок по платформам, прежний immutable release и результат переключения либо отката. Не выводите воспроизводимость повторной сборки из этих checksum.

Кандидатные workflow закрепляют исходный commit и записывают provenance и хеши. Повторная сборка того же source с последующим сравнением двух артефактов не описана и в receipts не подтверждена, поэтому эти хеши доказывают целостность скачанного кандидата, но не byte-for-byte воспроизводимость сборки. Для кандидатов Docker build context исключает `.env`, `secrets/`, `backups/` и `.storage`; репозиторий исключает `/config` и `.env*`, а Compose хранит runtime `/config` в отдельном named volume. Манифест кандидата проверяет состав, source и хеши, но не заменяет независимый secret scan всего образа.

Receipt приватной попытки wheel `20260930.4+habab.13` отдельно отмечает `wheel: NOT_CREATED` и `public_release: NOT_DONE` после ошибки lint; см. [receipt](../qa/issue-monitor-20261026/native13-private-build.json). Workflow **Habab native frontend candidate** создаёт только unpublished wheel-кандидат с манифестом source/upstream revisions и SHA-256 wheel/patch/catalog, с хранением один день. Ни candidate workflow, ни этот receipt не подтверждают новый публичный frontend-релиз.

`habab/build/upstream.patch` — архив исходной модификации, его не накладывают повторно. Источник истины следующих сборок — реальные `demo/`, `src/`, `build-scripts/` этого fork.

Docker Core из `habab/distribution/` пока основан на закреплённом официальном образе. Обновление frontend не обновляет Core или данные владельца. Автоматизация релизов: [issue #7](https://github.com/marsiandeployer/habab-home-assistant/issues/7).
