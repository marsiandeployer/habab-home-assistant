# Android and owner-test QA: current handoff

Checked 2026-10-24 UTC (2026-10-25 Moscow). Overall acceptance is incomplete.

| Target | Last verified state | Remaining proof |
| --- | --- | --- |
| Physical P30 | Matching-signer APK15 installed; old Core stopped cleanly; runtime2c extraction began | ADB18537 is now connection-refused. Extraction/rotation, server restart and configuration preservation after extraction remain unknown |
| Native15 candidate | Runtime2c/source6d9565be3, Habab0.3.6/Core2026.10.2; 70 JVM tests with full archive, assemble and lint passed | New physical voice, background operation and 48-hour stability are NOT RUN |
| Authenticated cloud owner-test | Active image22ae7a87/sourceaaeab54b5; 59 image source hashes match; Core RUNNING, Habab/Gemini selected and loaded; served chat SHA7f80a639 matches | Original inventory, risk, lifecycle and service scenarios on this newly deployed image remain pending with the web owner |
| Native HA setting | Conversation exposure for weather.forecast_pogoda_doma, originally changed through the visible chat, is still explicitly true after container replacement | This persistence check is cloud-only; the corresponding phone forecast has a different entity ID and has not been changed |
| Browser Gemini relay | Source commit1e87c1c6c; 30 local tests passed; browser mode defaults disabled | Relay deployment/configuration, backend browser lease, frontend and real browser audio remain pending |

The active cloud frontend/prompt is newer than the frozen Android runtime2c. They are separate release identities; cloud acceptance must not be described as a phone update. No physical microphone or acoustic voice result is inferred from source tests.

Current evidence:
- `owner22ae-active-readback.json`: active cloud image/source, authenticated baseline, rollback and served frontend.
- `owner22ae-candidate-parent-attestation.json`: all59 server files matched in the immutable candidate without home mounts or network.
- `owner22ae-exposure-persistence.json`: read-only native setting check; records the correction that native exposure lists omit false entries.
- `native15-phone-operator-update.json` and `native15-recovery-handoff.md`: partial phone update and recovery order. Inspect any still-running operator before starting Core.
- `native15-capability-matched-build.json`: build-time artifact proof. Its phone/publication fields describe that earlier point; they are not current installation status.
- `native15-runtime-publication.json`: published signed runtime files. This does not establish HTTPS phone installation or publish/promote the preview APK.
- `gemini-browser-relay-source-qa.json`: source-only relay tests and pending dependencies.
- `shared-demo-venv-repair.json`: the accidental shared environment recreation was repaired; fresh imports, live health and mock-state HTTP200 passed with original gunicorn processes unchanged.

The original14d cloud container is stopped and retained as `habab-owner-test-before-036-lifecycle-20261024T211653Z`. There is one running owner-test container. No service key, provider key, raw environment, token or user transcript is included in these receipts.
