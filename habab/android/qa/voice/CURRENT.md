# Android and owner-test QA: current handoff

Updated 2026-10-25 after physical P30 recovery and Native16 installation. Overall acceptance is incomplete.

| Target | Last verified state | Remaining proof |
| --- | --- | --- |
| Physical P30 | ADB18537 device; matching-signer APK16 installed, Core2026.10.2/Habab0.3.6 RUNNING. Four config/auth files preserved byte-for-byte before restart; runtime2c and previous711 retained. Installed launcher seeded guest loopback hosts. Prior-active VoiceService restored in fresh active/wake state | Real negative wake/farewell, acoustic interruption/echo, reboot/autostart and 48-hour acceptance remain incomplete |
| Native16 | APK sourcefc8a71a01; frozen runtime2c/source6d9565be3. Offline assemble/lint and focused guest-hosts tests passed; installed on P30 | Does not contain false-wake or farewell-policy fixes (#19/#20); preview APK is not promoted to public stable CTA |
| Authenticated cloud owner-test | Active image22ae7a87/sourceaaeab54b5; 59 image source hashes match; Core RUNNING, Habab/Gemini selected and loaded; served chat SHA7f80a639 matches | Original inventory, risk, lifecycle and service scenarios on this newly deployed image remain pending with the web owner |
| Native HA setting | Cloud forecast exposure persisted after container replacement. On P30 Native15, visible served web chat changed only weather.forecast_home_assistant Conversation exposure false→true; native readback and actual OpenCode assist_exposure set/get trace verified | Phone-served desktop browser proof is not a touch test of the Android WebView. Recheck semantics after future runtime upgrades |
| Browser Gemini relay | Source commit1e87c1c6c; 30 local tests passed; browser mode defaults disabled | Relay deployment/configuration, backend browser lease, frontend and real browser audio remain pending |

The active cloud frontend/prompt is newer than the frozen Android runtime2c. They are separate release identities; cloud acceptance must not be described as a phone update. No physical microphone or acoustic voice result is inferred from source tests.

Current evidence:
- `native16-telegram-parent-checkpoint.json` and attempt1/2 receipts: migration acceptance incomplete, no pairing/question/model call. Attempt2 UI configure failed before an entry was accepted; exact cloud entry was positively restored after phone inactivity proof. No raw transcript/key persisted.
- `native16-guest-network-preflight.json` and `native16-guest-telegram-ipv4-tls-readback.json`: credential-free guest probes distinguish working weather HTTPS from Telegram IPv4 TCP timeout. This narrows transport troubleshooting but does not retrospectively establish the exact lost UI error.
- `native16-background-lifecycle-readback.json`: both Android services foreground, own server/voice wake locks present, package already on Doze whitelist; USB powered. Huawei vendor autostart/reboot acceptance is not proved.
- `native16-selected-preferences-readback.json`: voice enabled with configured phrase эй дом; permanent SSH/HTTPS tunnel disabled/unconfigured. The proposal in `../network/public-https-reverse-tunnel-plan.md` has not been applied.
- `native19-final-result-parent-qa.json`: future wake candidate ignores partial hypotheses, finalizes before VAD reset; Gradle83passed/1archivecase skipped and lintPASS. Not built into the installed Native16; acoustic/latency acceptance NOT RUN.
- `native16-voice-stop-start-readback.json`: stopped idle VoiceService publishes valid inactive/off status; service restored in wake with screen off and unchanged Core PID. First harness attempt treated Android stop-command exit255 as failure; it is preserved separately, and the corrected observer requires positive native service/state readback.
- `native16-core-lan-alias-readback.json`: authenticated CoreRUNNING and homeassistant.local announced; another LAN client, OAuth return and external URL remain unverified.
- `native16-status-pdf-20261025.json`: refreshed two-page physical report, both rendered pages inspected.
- `native16-background-status-readback.json`: 20/20 valid snapshots over34.19s, screen off and active/wake throughout, capture advanced; this is a short background check, not acoustic or 48-hour acceptance.
- `native16-phone-apk-update.json`: physical guarded APK-only update, installed launcher hosts seed, four-file preservation, unchanged runtime, Core readiness, restored voice and temporary-session revocation.
- `native16-candidate-build.json`: frozen clean-source artifact SHA/signature/input/build validation.
- `native15-feedback-observation-20261025.json`: latest user-requested voice history inspection; feedback marker recognized, assistant invited details, no upload/issue creation proved. Transcript was not copied into the receipt.
- `native15-voice-metadata-after-user-reports.json`: actual native Gemini Live connection/replies with screen off and completed END-cue metadata. User reports false wake and delayed farewell; these are not acoustic PASS.
- `native15-recovered-runtime-readback.json` and `native15-recovered-owner-baseline.json`: recovered runtime payload and semantic owner/provider/router/shopping baseline.
- `native15-exposure-tool-trace.json`: completed actual phone OpenCode set/get calls for the forecast exposure scenario.
- `native15-telegram-live-preflight-20261025.json`: bot remains owned by the cloud installation; P30 has no bot entry. Owner identity matches the existing private Telegram binding; no migration or message was performed.
- `relay-browser-reenrollment-preflight.json`: original authorized operator credential recoverable in RAM; Google catalog200 includes configured text/live models. No key printed/new credential file, no relay restart or inference.

Historical evidence below describes its recorded checkpoint; later recovery supersedes the old transport/installation blockers:
- `native-status-android-compile-qa.json`: clean sourcee83e22906 compiled and linted offline against AndroidSDK34 in an isolated RAM workspace; old2c inputs pinned, noAPK or device action.
- `phone-bridge-cleanup.json`: unused parent HTTP bridge18124 stopped with zero clients; recreate only after transport recovery.
- `native-status-publication-source-qa.json`: future native status writer uses atomic replacement and final off/error publication,3 focused JVM tests and Java8 API compilation passed. Interrupted-write cause was not reproduced on the host; fix is not in installedAPK15 and has no Android device proof.
- `../../../qa/shopping-list-e2e-20261024/owner22ae-native-v2-bootstrap.json`: fresh native_v2 session/context HTTP200 on22ae, exact exposed cloud forecast, Gemini3.8 Live through Habab, observed credential TTL60s. No Live/audio/model conversation was performed.
- `owner22ae-active-readback.json`: active cloud image/source, authenticated baseline, rollback and served frontend.
- `owner22ae-candidate-parent-attestation.json`: all59 server files matched in the immutable candidate without home mounts or network.
- `owner22ae-exposure-persistence.json`: read-only native setting check; records the correction that native exposure lists omit false entries.
- `native15-phone-operator-update.json` and `native15-recovery-handoff.md`: partial phone update and recovery order. Inspect any still-running operator before starting Core.
- `native15-capability-matched-build.json`: build-time artifact proof. Its phone/publication fields describe that earlier point; they are not current installation status.
- `native15-runtime-publication.json`: published signed runtime files. This does not establish HTTPS phone installation or publish/promote the preview APK.
- `gemini-browser-relay-source-qa.json`: source-only relay tests and pending dependencies.
- `shared-demo-venv-repair.json`: the accidental shared environment recreation was repaired; fresh imports, live health and mock-state HTTP200 passed with original gunicorn processes unchanged.

The original14d cloud container is stopped and retained as `habab-owner-test-before-036-lifecycle-20261024T211653Z`. There is one running owner-test container. No service key, provider key, raw environment, token or user transcript is included in these receipts.
