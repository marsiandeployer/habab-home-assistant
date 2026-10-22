# Сборка fork

Baseline: upstream commit 92599a88037ef5b7bd822efd7301028d824f7f5b, Apache-2.0. Модификации русского демо уже внесены в корневые demo/, src/, build-scripts/.

Из корня: sh habab/build/build.sh. Сборка использует текущий checkout, повторно накладывать upstream.patch не нужно. Publisher создаёт версионированные файлы в habab/public/releases/ и атомарно переключает index.html. Старые релизы остаются для открытых вкладок.

Русский перевод исходного выпуска: source/ru.json, SHA-256 225a190e2c924b292fbddaee73fb684201a57c5eecbea78044c949bdf91bfe57. После обновления upstream проверьте перевод и требования к Node/pnpm.

Форму Telegram publisher включает отдельным script; минифицированные чанки не меняются. Карта использует фиксированный серверный tile cache, не offline-архив. API состояния находится в habab/.

[Обновления](../docs/UPDATING.md) · [Компоненты](../README.md)
