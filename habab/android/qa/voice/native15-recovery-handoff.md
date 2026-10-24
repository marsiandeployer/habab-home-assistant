# Native15 P30 recovery handoff

Status: PARTIAL; transport unavailable. This is not physical release acceptance.

Verified before the connection stalled:
- Huawei P30 VOG-L29 authorized over ADB 18537.
- VoiceService stopped; Android ActivityManager showed service absent.
- Java supervisor paused; authenticated Home Assistant stop completed; exact Core process exited before package force-stop.
- Matching-signer APK versionCode 15 installed.
- Installed-APK operator started verification/extraction of pinned runtime 2c6567223ad3ef6b.
- Last operator observation: dalvikvm active, CPU time progressing at approximately 153 seconds.

Unverified after transport stall:
- Signature/extraction success, runtime rotation, previous-runtime retention.
- Post-extraction preservation of the four configuration/auth files. Before hashes existed only in the failed process RAM; no comparison completed.
- Core restart, temporary QA owner-session revocation, chat, voice or background operation.

Host maintenance is terminal. Its pending app-start command was canceled when all ADB requests began timing out. No suspended host updater remains. A listening TCP port is not ADB acceptance: even devices listing timed out.

Recovery order once transport returns:
1. Read-only inspect APK version, exact runtime/staging/previous manifests and same-UID operator process. Do not start Core while an operator is still extracting or rotating.
2. If runtime2c finished, verify all 59 pinned sources and frontend payload against signed release metadata before server acceptance. Retain previous runtime711.
3. If staging is incomplete, preserve and inspect it. Do not rerun the original native14 updater against the now-installed APK15. Any new extraction must leave configuration untouched and have a separate bounded recovery receipt.
4. Start verified runtime, authenticate owner safely, check Core RUNNING/Habab0.3.6, existing grocery item and provider settings. Verify or revoke only the exact QA session; do not revoke unrelated sessions.
5. Only then run physical chat exposure test for weather.forecast_home_assistant, native readback, fresh realtime context and bounded screen-off check. Acoustic voice acceptance remains NOT RUN.

The private original updater now refuses any initial APK other than14 or runtime other than711, allows a bounded900s extraction, treats transport failure as unknown rather than process-exit proof, and does not auto-start Core after an unverified extraction. Syntax checked only; it has not been rerun and is not a recovery installer.
