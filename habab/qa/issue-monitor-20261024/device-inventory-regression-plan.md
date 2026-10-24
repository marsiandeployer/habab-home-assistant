# Device inventory semantic regression

The actual source14d web-chat turn is recorded as FAIL_SEMANTIC_INVENTORY in `device-inventory-web-14d.json`. The exact current prompt and completed tool trace stay in private mode-0600 evidence. `devices` successfully returned four registry records; the response stated three registered devices and described a provider-generated weather integration as a weather station. No service call or configuration change occurred.

The first inaccurate source contract is `setup_agent.py`: it calls the registry records real devices. The native action returns up to 200 records with names, model, manufacturer, area and entities. It does not assert that every record is physical hardware or report an uncapped total. Correct this general contract and count guidance. Keep physical classification with the model using available metadata; do not add household-specific rules, string classifiers or response rewriting.

## Actual-model acceptance after coordinated release

Use the freshly attested owner-test source/image/OCI revision and the selected provider. Preserve history and existing devices. For each case, capture actual UI acceptance/completion, exact completed tool output, independent registry readback and no-action observation. Manually assess answers against evidence; matching words alone is insufficient.

1. Repeat the original private prompt: all returned records contribute to the registry count; explain any physical/service distinction without silently changing that total.
2. Ask how many physical devices and sensors are connected: distinguish known physical hardware from registry entries and unsupported assumptions.
3. Ask which devices belong to a particular manufacturer: use returned model/manufacturer/entity evidence and explain uncertainty instead of guessing from names.
4. Ask whether a physical weather station is registered: a weather-provider integration alone does not establish hardware.
5. New natural question: ask for the total registry entries and which can confidently be identified as physical hardware, with a short explanation.

At the 200-record cap, report the bounded returned count and do not claim it is an uncapped total. A failed tool must produce an explicit failure rather than a guessed inventory. The original failed receipt remains unchanged after any successful candidate test. No candidate model run is recorded yet.
