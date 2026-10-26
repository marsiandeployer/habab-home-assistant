# Home Assistant Frontend Agent Guide

You are helping develop the Home Assistant frontend. This repository is a TypeScript application built from Lit-based Web Components for the Home Assistant web UI.

## Essential Commands

```bash
pnpm lint          # ESLint + Prettier + TypeScript + Lit
pnpm format        # Auto-fix ESLint + Prettier
pnpm lint:types    # TypeScript compiler, run without file arguments
pnpm test          # Vitest
pnpm dev           # App dev server, supports --background/--status/--stop/--logs
pnpm dev:serve     # Local serving dev server, supports -c core URL, -p port, and dev flags
```

Never run `tsc` or `pnpm lint:types` with file arguments. When `tsc` receives file arguments, it ignores `tsconfig.json` and can emit `.js` files into `src/`. Always run `pnpm lint:types` without arguments. For individual file type checking, rely on editor diagnostics.

## Architecture

- The frontend uses custom elements built with Lit and TypeScript strict mode.
- Components communicate with the backend through the Home Assistant WebSocket API.
- Use `ha-` for Home Assistant components, `hui-` for Lovelace UI components, and `dialog-` for dialogs.
- Prefer `ha-*` components and current Web Awesome wrappers. Avoid adding new legacy `mwc-*` usage.
- Leaf components should consume narrow Lit contexts instead of taking the broad `hass` object unless they are containers that own and provide `hass`.

## Приоритеты беты Habab Home

- Это бета: главный приоритет — чтобы работали основные пользовательские сценарии. Выбирай минимальное простое решение, не оверинженерь.
- Проверяй основной сквозной сценарий. Пограничные случаи, дополнительные абстракции, инфраструктуру и расширенные матрицы тестов добавляй только для конкретной воспроизведённой проблемы или по прямому поручению пользователя.
- Функциональность важнее дополнительного усиления безопасности. Не расширяй задачу отдельными механизмами маскирования секретов, аудитами безопасности и многослойными защитами без конкретной необходимости или отдельного поручения. Такие улучшения не должны задерживать рабочую основную функцию.

## Development Standards

- Use strict TypeScript, proper interfaces, and `import type` for type-only imports.
- Avoid `any`; model data with existing Home Assistant types or narrow new types.
- Keep imports organized and remove unused imports.
- Do not use `console`; use existing logging or user-visible error patterns.
- Use `@state()` for internal Lit state and `@property()` for public API.
- Do not query or manipulate DOM manually when Lit decorators, component refs, or render state are appropriate.
- Scope styles to components, use theme custom properties, and keep layouts mobile-first and RTL-safe.
- All user-facing text must be localized through the translation system.
- Do not write tests just because you changed some code. Write a test when there is real logic that could break without anyone noticing, and explain what the test protects.

## Habab Home product documentation

- Every new user-facing Habab Home feature or integration must have corresponding user documentation on **habab.ru**. A README or technical note in this repository alone does not satisfy this requirement. Create or update the relevant page under `/root/hababru/content/products/ai_home_server/` (or the owning product source), using the existing Telegram setup material as a reference.
- Explain how a user enables and uses the feature, its prerequisites, and material limits or privacy implications. Write clear, appealing product copy, while keeping availability and behavior consistent with the code and verified acceptance; label preview or unverified behavior plainly.
- Before changing the habab.ru source, read `/root/hababru/AGENTS.md` and follow that project's source/build/publish workflow. Edit source content, never generated `static_build/` output by hand.

## Выпуск Android Habab Home

- Каждый релиз Android должен сначала заново компилировать native frontend из того же проверенного source SHA, затем включать этот wheel в подписанный runtime и собирать APK. Не подставлять ранее опубликованный frontend только по номеру версии. Передавать `--frontend-candidate` упаковщику; сверять SHA wheel, source SHA и producer manifest с подписанным runtime и извлечёнными данными APK. В workflow задавать явные `android_version_code`/`android_version_name` для нового проверенного выпуска; не использовать старые Gradle defaults. Проверять версию подписанного APK через `aapt` и его фактический URL нового runtime. Подготовка APK со старым runtime для отдельной проверки не является полным релизом. Команды и signing prerequisites — `habab/docs/RELEASE-CANDIDATE.md`.

## ADB для пользовательского Huawei P30

- Используй выделенный ADB-сервер: `/opt/android-sdk/platform-tools/adb -H 127.0.0.1 -P 18537 devices -l`. Serial выбирай по свежему списку. Пустой результат стандартного `adb devices` не означает, что телефон отключён.
- Проверяй ADB и HTTP-мост отдельно: состояние `device` подтверждает ADB, а HTTP 503 на `127.0.0.1:18124` относится к мосту/доступности Home Assistant. Не выдавай одно за состояние другого.

## Временные Docker-стенды Habab

- Перед запуском тестового HA/Core проверь `docker ps -a`, `free -h` и текущую нагрузку. Найди существующий стенд и его владельца; согласуй использование общего стенда с владельцем.
- По умолчанию используй один временный QA-контейнер на серию совместимых проверок, в том числе нескольких фич одного HA/Core образа. Проверяй фичи последовательно и сбрасывай их синтетическое состояние между сценариями; сохраняй нужные результаты до сброса. Не создавай отдельный контейнер на каждую фичу и не накапливай работающие копии `retry2`, `retry3` и далее: переиспользуй свой стенд или сначала останови и удали предыдущую попытку. Отдельный стенд допустим только при несовместимых версиях/зависимостях или доказанной изоляционной необходимости; параллельные стенды — только для конкретного сравнения с заранее рассчитанным общим бюджетом ресурсов.
- Каждый временный контейнер запускай с явными лимитами `--memory`, `--memory-swap`, `--cpus` и `--pids-limit`. Выбирай значения под проверку и доступный запас хоста; не запускай стенд без лимитов.
- Записывай ID контейнера и владельца сессии. На успехе, ошибке, timeout и отмене выполняй очистку в `finally` или shell `trap`; после неё проверь, что собственный контейнер и его дочерние тестовые процессы завершены. Постоянный QA-стенд оставляй работающим только с назначенным владельцем и явной причиной.
- Чужие контейнеры, общие стенды и их данные не останавливай и не удаляй без согласования с владельцем. При нехватке RAM сначала заверши свои временные попытки; новый retry до освобождения ресурсов не запускай.
- Каталоги сборки и runtime-cache в `/dev/shm` занимают RAM. Учитывай их в бюджете памяти и убирай свои промежуточные копии после проверки; крупные сборки по умолчанию размещай на диске с проверенным запасом места.

## Project Skills

Detailed guidance lives in `.agents/skills/<name>/SKILL.md`. Load every matching skill before detailed implementation or review. For reviews, load `ha-frontend-review` alongside all companions that apply to the changed code or behavior:

- `ha-frontend-contexts`: Lit contexts, `hass` migration, and rerender-sensitive state access.
- `ha-frontend-components`: dialogs, forms, alerts, shortcuts, tooltips, panels, and Lovelace cards.
- `ha-frontend-events`: event handler typing, custom event dispatch, and event-map declarations.
- `ha-frontend-types`: backend data contracts, optional schemas, shared types, assertions, and lifecycle types.
- `ha-frontend-lit`: reactive fields, DOM queries, lifecycle behavior, and render-derived state.
- `ha-frontend-styling`: theme variables, spacing tokens, responsive layout, RTL, and view transitions.
- `ha-frontend-testing`: lint, typecheck, Vitest, Playwright e2e dev servers, and benchmarks.
- `ha-frontend-query-params`: typed URL query params, `QueryParamsController`, data table URL sync, in-app links, and My Home Assistant redirect params.
- `ha-frontend-user-facing-text`: localization, terminology, sentence case, and Home Assistant text style.
- `ha-frontend-review`: PR template use, existing review feedback, review checklist, recurring issues, and UI/UX evidence including gallery specifications.
- `ha-frontend-ux-readiness`: UI/UX routing that complements frontend review for new user experiences.
- `ha-frontend-gallery`: gallery pages, demos, sidebar structure, content, and verification.
- `ha-frontend-demo`: standalone demo structure, configurations, navigation, shared stubs, and verification.

## Pull Requests

When creating a pull request, use `.github/PULL_REQUEST_TEMPLATE.md` as the PR body. Preserve template sections, check only the appropriate type-of-change boxes, and do not check checklist items on behalf of the user. If the PR includes UI changes, remind the user to add screenshots or a short video.

## AI policy

This project follows the [Open Home Foundation AI Policy](AI_POLICY.md).
Autonomous contributions are not accepted: a human must review, understand,
and be able to explain every change before it is submitted. Do not open
issues or pull requests autonomously, and do not post comments on behalf of
a user without their review.
