# Telegram setup: contract and quality bar

## Direction contract

Mode: Operate. Scope: one independent demo-only Telegram setup component inside the existing Home Assistant demo. This is an extension, not a replacement world. No new concept roll, marketing world or approved raster comp is needed.

FORM seed: Home Assistant's existing dialog/form/control vocabulary, corroborated by upstream `src/components/ha-dialog.ts`, `src/components/ha-form/ha-form.ts`, and the required dialog/form conventions in `.agents/skills/ha-frontend-components/SKILL.md`. The native dashboard screenshots captured before this feature corroborate the surrounding world. The implementation uses platform-native dialog and HTML form inside the independent Habab addon; it does not import private compiled HA modules.

TYPE: inherit the demo's Roboto/system typography, 14px body, restrained 22px dialog title, readable helper text. MATERIAL: white form surface, one border, 12px outer radius, no decorative shadows/gradients. GROUND: preserve the existing Home Assistant dashboard; backdrop signals the modal state only. Accent is a legible blue for controls; native blue references remain visible behind the modal.

Structure: Telegram launcher → titled dialog → purpose → labelled password input and show/hide → demo scope → cancel/connect → explicit demo success. Mobile wraps the reveal control below the input while preserving order. No new navigation or permanent dashboard redesign.

Required viewports: 1280×800 and 390×844. States: initial, invalid format, show/hide, cancellation, demo success, close/Escape and reopen. Token is never interpolated into markup, requests, URL, storage or status copy. Success never implies a real bot connection.

## Quality bar card: native Home Assistant extension

- Looks like a restrained tool within the current HA surface: familiar typography, label hierarchy, blue controls and a quiet neutral dialog.
- One task per dialog; primary action remains visible and the field/help text are readable at both widths.
- Keyboard access, visible focus, labels, alert/status announcements, native focus containment and Escape work.
- Dialog fits the viewport, including the narrow mobile layout; no clipped controls or horizontal overflow.
- Token stays masked by default, is cleared on close/success, and never reaches network or browser storage.
- Explicit demo status before submission and after success; no false live-connection claim.
- Native dashboard remains usable after closing; state API and persistent virtual devices continue working.

Acceptance evidence: the initial and success desktop/mobile captures in `qa/`; `telegram-browser.json` verifies the interaction and token boundaries. Mechanical detector returned an empty finding list. These checks assess only the new form; full upstream lint/build is separate from importing the previously built demo baseline.

## Shipped implementation note

The form keeps the existing dashboard composition intact in the supplied desktop and mobile captures. Its isolated shadow-root stylesheet currently sets its own accent (`#006b8f`), text colors, control sizing, and dialog/control radii rather than reading Home Assistant theme variables. The surrounding dashboard uses the native theme, including `--primary-color` and the shared Roboto typography tokens (`--ha-font-family-body`, `--ha-font-size-m`). Treat the form's fixed values as scoped implementation details, not new Habab or Home Assistant tokens. This theme-inheritance difference is recorded for future reconciliation; it was not changed in this documentation pass.
