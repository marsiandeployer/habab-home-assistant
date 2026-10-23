# Обновление Home Assistant

Это настоящий GitHub fork `home-assistant/frontend` с общей историей Git. `origin` — `marsiandeployer/habab-home-assistant`, `upstream` — `home-assistant/frontend`. Наша ветка — `main`, upstream использует `dev`. Исходный baseline — `92599a88037ef5b7bd822efd7301028d824f7f5b`.

Изменения русского демо внесены отдельным коммитом в upstream-исходники. API, Docker-дистрибутив и независимые формы находятся в `habab/`.

## Frontend Docker-дистрибутива

Docker Core остаётся на 2026.10.2. Полный frontend собирается из Core-совместимого upstream tag `20260930.4` с патчем `habab.3`; это отдельная сборка от публичного демо в `habab/build/`. Wheel версии `20260930.4+habab.3` публикуется по неизменяемому HTTPS URL с SHA-256 в пути. При `docker build` `install-native-frontend.py` сверяет закреплённые версию, имя файла, размер, origin и digest, затем устанавливает wheel без дополнительных пакетов. В том же build находятся API bridge v1 для глобального чата и native `step-flow-form`; context host использует HA `contextMixin` и сохраняет native callbacks, переводы, selectors и валидацию. Сбой загрузки или несовпадение digest останавливают сборку.

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
6. Проведите review, слейте ветку и выпустите версионированные артефакты. Не используйте существующее имя публичного релиза для изменившихся чанков.

`habab/build/upstream.patch` — архив исходной модификации, его не накладывают повторно. Источник истины следующих сборок — реальные `demo/`, `src/`, `build-scripts/` этого fork.

Docker Core из `habab/distribution/` пока основан на закреплённом официальном образе. Обновление frontend не обновляет Core или данные владельца. Автоматизация релизов: [issue #7](https://github.com/marsiandeployer/habab-home-assistant/issues/7).
