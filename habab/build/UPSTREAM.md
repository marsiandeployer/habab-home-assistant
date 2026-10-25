# Сборка fork

Baseline: upstream commit 92599a88037ef5b7bd822efd7301028d824f7f5b, Apache-2.0. Модификации русского демо уже внесены в корневые demo/, src/, build-scripts/.

Из корня: sh habab/build/build.sh. Сборка использует текущий checkout, повторно накладывать upstream.patch не нужно. Publisher создаёт версионированные файлы в habab/public/releases/ и атомарно переключает index.html. После успешной публикации общий release-retention удаляет релизы старше текущего и предыдущего. На новом хосте сначала инициализировать манифест текущего и предыдущего релизов; без него очистка пропускается.

Для уже собранного внешнего `demo/dist` передайте publisher-у каталог и SHA исходного checkout:

```sh
HABAB_DEMO_RELEASE="$RELEASE_SLUG" python3 habab/build/publish_build.py \
  --source-dir "$COMPILED_DEMO_DIST" \
  --frontend-source-revision "$FRONTEND_SOURCE_SHA"
```

Задайте переменным отдельный slug, каталог результата и полный 40-символьный SHA до запуска. Каталог должен содержать `index.html`, `assets/`, `frontend_es5/`, `frontend_latest/`, `static/`, `stub_config/`, `_headers` и `manifest.json`. Publisher добавляет Apache-2.0 `LICENSE.md` из этого checkout, не копирует соседние файлы и записывает fork revision отдельно от upstream baseline. Этот режим несовместим с `--from-published` и `--entrypoint`.

Русский перевод исходного выпуска: source/ru.json, SHA-256 225a190e2c924b292fbddaee73fb684201a57c5eecbea78044c949bdf91bfe57. После обновления upstream проверьте перевод и требования к Node/pnpm.

Форму Telegram publisher включает отдельным script; минифицированные чанки не меняются. Карта использует фиксированный серверный tile cache, не offline-архив. API состояния находится в habab/.

[Обновления](../docs/UPDATING.md) · [Компоненты](../README.md)
