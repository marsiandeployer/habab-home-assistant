# Native form focus and chat history rendering

`check-behavior.cjs` loads the current downstream `setup-chat.js` in an isolated
BrowserContext on the existing CDP daemon. It mocks only public chat history and
creates nested shadow-root controls matching the native flow-form / `ha-form`
boundary. It checks editable and non-editable focus, mobile split/collapse,
desktop split, and the delayed `ha-markdown` registration path. It does not
authenticate, submit a form, call Home Assistant, or touch another browser
context.

Run from `/root/homeassistent`:

```sh
node habab/qa/native-form-mobile/check-behavior.cjs
```

The report is written to `behavior-report.json`. The 390x844 viewport is browser
emulation; it does not open a physical Android keyboard or validate keyboard
driven `visualViewport` resizing.

## Native Home Assistant check

`check-native-ha.cjs` is a separate authenticated check against the owned test
installation on localhost8403. It requires that private fixture and its token; it
is not a test for an arbitrary Home Assistant. It starts a Local To-do flow,
focuses the real `step-flow-form` input, verifies expansion to844px and dashboard
`inert`, then verifies collapse to422px and cancels the unsubmitted flow. It does
not configure a physical device or validate a physical Android keyboard.

The accepted backend snapshot was c49657165, with the candidate UI file mounted
read-only. Runtime readback confirmed Core2026.10.2, frontend
20260930.4+habab.3, and UI SHA256 b565aebcc7976cf9c6bdfa956c027197f98070095746fca318dc59dae73dd9b0.
The browser reported no JavaScript errors and no plaintext assistant message
fallbacks after loading the existing history. Its own context was closed and the
temporary form cancelled. `native-ha-report.json` records this separately from
the synthetic fixture.
