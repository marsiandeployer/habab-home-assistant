# Issue #19: finalized wake phrase source candidate

Status: **source-only candidate**. Baseline Native16 source identity from the local candidate receipt: `fc8a71a017a01eca469a0c2b050ce0add11f0741`; parent identified the installed Native16 wake detector as unchanged from the prior source. This patch is uncommitted. No APK build/install, device, microphone, provider, or API action was performed. No raw audio or recognized text is logged. Physical/acoustic reproduction remains **NOT RUN**.

## Upstream behavior checked

The official Vosk Java API describes `acceptWaveForm()` returning true when silence ends an utterance and `getResult()` can be retrieved; false means decoding continues. It describes `getPartialResult()` as an interim hypothesis that is not finalized and can change, and `getFinalResult()` as flushing remaining feature-pipeline audio. The official Java example selects result versus partial from `acceptWaveForm()` and obtains `getFinalResult()` after stream end:

- [Vosk `Recognizer.java` API comments and methods](https://github.com/alphacep/vosk-api/blob/master/java/lib/src/main/java/org/vosk/Recognizer.java#L1017-L1135)
- [Vosk `DecoderTest.java` stream loop and final flush](https://github.com/alphacep/vosk-api/blob/master/java/lib/src/test/java/org/vosk/test/DecoderTest.java#L495-L510)

This candidate uses those documented semantics; it does not copy upstream implementation code.

## Change

- `WakeMatchGate` is a pure Java decision helper. It keeps the existing NFKC/Russian-lowercase/punctuation normalization and whole-string equality. Partial results never match; an exact endpoint result or exact VAD-end final may match once per result cycle.
- `WakeDetector` no longer reads `getPartialResult()` or uses the previous 200 ms partial-match timer.
- When the VAD gate ends speech, the detector calls `getFinalResult()` before resetting. This flushes residual Vosk frames rather than dropping a phrase that had not yet endpointed during the VAD hangover.
- Pre-roll frames now use the same endpoint-result path and are checked for finalized matches instead of discarding any endpoint result generated while replaying pre-roll.
- The existing configured phrase remains supported: the same normalization, 1–5-word/64-character validation, dictionary-word validation, grammar construction, and exact complete normalized text comparison remain in place.
- Recognizer/result-gate state resets after endpoint results, VAD end, the 15-second recognizer reset, an accepted wake, and the existing service error/session resets.

## Tests and build

Five isolated JVM tests cover partial-exact then wrong final, partial-exact then matching final, matching VAD-end final, duplicate final suppression/reset for the next utterance, and normalized custom phrase/whole-string boundaries. Manual `javac` + JUnitCore run: **5 passed**. `:app:compileDebugJavaWithJavac` with Gradle offline: **PASS**; no APK task was invoked.

The targeted Gradle unit-test command did not reach test execution because existing unrelated `GuestHostsFileTest.java` and `VoiceStatusFileTest.java` use `Files.readString`/`Files.writeString`, which are unavailable in the Android test compile API here. The new pure helper and its test were compiled/run independently from `/dev/shm`; temporary classes were removed automatically.

## Limits and risk

This removes activation from an unstable partial; it does not eliminate false activation if Vosk's finalized result itself is an exact false recognition. Waiting for a finalized result may add wake latency up to the recognizer's SHORT endpoint or the VAD-end flush. No controlled negative-audio fixture or P30 acoustic test was run, so neither latency nor false-wake rate is established.

## Source hashes

- `WakeDetector.java`: `494b2a5340eda65d388952054fd1ecde17672654ef94a7fe12bfa613933b6778`
- `WakeMatchGate.java`: `5678c737bbeb475eb850d5187c61584409809e6ce118e8af497799907d54a18f`
- `WakeMatchGateTest.java`: `3dc86adaba5967ef9062cdbbfb97a876920f89e8878a3d6e832401c68f94ff23`

No files staged or committed.

## Parent follow-up validation

The parent replaced unavailable newer Java file/collection helper APIs in the two pre-existing tests with equivalent portable reads/writes/collections. No assertions were removed. After that change, the actual Gradle `:app:testDebugUnitTest :app:lintDebug` command completed successfully offline: 17 suites, 84 discovered tests, 83 passed, one skipped archive case because no full runtime archive was supplied. All five wake tests ran and passed through Gradle. This supersedes the earlier test-task blocker above; the original failed attempt remains recorded. No APK task or device deployment of this source candidate was performed.
