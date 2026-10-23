# Canonical assistant acceptance — Habab Home 0.3.0

The administrator dashboard, native Assist shortcut and AI settings open the
same floating chat. Web and Telegram delegate to `AssistantService`, retain
their established separate session stores, and share the private OpenCode
runtime. Native Assist keeps Home Assistant's exposed-tool permission boundary.

Integration forms reuse the upstream `step-flow-form`, its field callbacks and
validation, inside a native context provider. Values travel directly to the
owner-scoped setup API. Model summaries contain bounded field names, types and
required flags; they exclude field values, defaults, selector options and URLs.

| Check | Result |
| --- | --- |
| Python tests in the final baked image | 100 PASS |
| Add-on JavaScript tests | 26 PASS |
| Matched frontend tests | 10 PASS |
| Frontend ESLint, formatting and pre/post-build types | PASS |
| Browser scenarios, desktop 1280×800 and mobile 390×844 | 7 PASS, no page errors |
| Native Time & Date form selection and submit | Entry loaded; real time sensor state verified |
| Synthetic date/time/entity/area/icon/template/action selector fixture | Rendered with native contexts; never submitted |
| Cold startup without Recorder | No failed WS commands and no `recorder/info` request |
| Native form lifecycle on status refresh | Disconnect count reduced from 1 to 0 |
| Windows ZIP | 71 allowlisted files; exact source and public body SHA verified |
| Secret scanning | Source and Windows ZIP clean; two unchanged official translation matches in the frontend wheel |

The seven-query model run is recorded separately in
`model-benchmark-final.json` as `PASS_WITH_LIMITS`. An empty continuation was
rejected with HTTP 400 and left the protected form pending. The final UI image
has byte-identical Python backend files to that model-test image; the later UI
change preserves mounted form elements during refresh. Full traces and
credentials remain outside the repository.

The actual Windows 0.3 upgrade, a live Telegram bot, a physical microphone and
physical smart-home devices are not covered by this acceptance. Telegram's
protected form steps currently hand off to the Home Assistant web chat.
Earlier Windows 0.2 UI checks passed, but its OpenCode `model_error` remains an
unresolved installation-specific finding until fresh access is available.

All included screenshots show a disposable QA home, not a personal Windows
installation. `release-check.json` records the final image and backend hashes;
`native-build.json` records the pinned frontend source and wheel. Earlier
model rounds and the Recorder startup reproduction are retained as evidence.

Run the browser check against a disposable owner on port 8403:

```sh
HABAB_QA_PRIVATE=/path/to/private-qa-owner node habab/qa/canonical-assistant/check-browser.cjs
```

The private directory must contain that QA owner's token JSON. The script
refreshes its token, creates a Time & Date integration, and writes the public
summary/screenshots. Do not point it at a personal installation.
