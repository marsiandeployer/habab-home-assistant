# Web setup through the shared assistant

Result: PASS for one native integration on the isolated test installation.
Physical devices and Telegram were not part of this check.

The container used the baked shared-chat0.3.1 image with a read-only source overlay
exported from commit66b42e48b. Its port8403 was bound to localhost. The owner-test,
Windows installation, and real Xiaomi vacuum were not changed.

The actual DeepSeek/OpenCode assistant opened the Local To-do config flow from a
web chat request. The protected card rendered Home Assistant's `step-flow-form`
inside `habab-native-form-context`. After reloading the page, the test entered a
list name in the native field and clicked Create. The backend reported `done`,
a loaded config entry, and one entity. A fresh native state query returned0
for the new to-do entity. A second real assistant turn correctly confirmed that
integration and empty list without making further changes.

The first finishing verifier read an old `hass.states` snapshot and reported a
missing state. Its failed result is preserved as `initial-finish-result.json`.
The resumed check used the native `get_states` WebSocket command and passed;
`finish-result.json` explicitly records the resumed run. No product fix was made
to force that assertion to pass.

Browser JavaScript errors:0. Screenshots were opened and inspected. The test's
browser contexts were closed, and its container was stopped after acceptance.

Remaining UX observations: a history reload can show Markdown markers until the
native Markdown element registers or another turn redraws the history; the model
also includes internal field names and capability numbers in ordinary replies.
These do not invalidate the native setup result, but they need a separate polish
pass. Physical Android keyboard behavior was not checked here.

Reproduction: refresh the private token for the owned loopback8403 test install,
run `node habab/qa/web-native-setup/check-web-native.cjs`, then run
`node habab/qa/web-native-setup/finish-web-native.cjs`. The fixture creates a Local
To-do entry only on that installation. To resume after a completed submission,
use `HABAB_QA_RESUME=1` for the finishing script. The expected evidence is a loaded
entry, to-do state0, and an assistant confirmation matching the native API.
