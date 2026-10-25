# Issue #19: current wake-source verdict

**Verdict: no source-only defect demonstrated; no code changed.** The reported false wakes remain an acoustic/runtime acceptance issue. This review read source and existing QA handoff only; it did not access the phone, microphone, runtime, account, or server, and it collected no audio or utterances.

## Current source evidence

- `WakeMatchGate.accept()` rejects `PARTIAL` and null result kinds, then allows at most one finalized result until reset. It compares the complete normalized result to the complete configured phrase (`habab/android/app/src/main/java/ru/habab/home/WakeMatchGate.java:22-28`). Normalization folds Unicode/case and punctuation while preserving word boundaries (`:15-20`).
- The detector accepts Vosk endpoint text and the VAD-end `getFinalResult()` only (`habab/android/app/src/main/java/ru/habab/home/WakeDetector.java:54-59,68-79`). The recognizer grammar is the configured phrase plus `[unk]`; phrase length and dictionary words are validated at construction (`:27-39`). VAD only gates audio and does not itself match a phrase (`habab/android/app/src/main/java/ru/habab/home/VoiceActivityGate.java:3-15`).
- Existing `WakeMatchGateTest` covers exact partial followed by a different final, exact endpoint/VAD-end acceptance, one-shot behavior/reset, and full normalized phrase/word-boundary matching (`habab/android/app/src/test/java/ru/habab/home/WakeMatchGateTest.java:7-44`).
- Therefore the prior repeated-partial activation path is absent in this source. An unrelated sound can still wake the service if Vosk produces the exact configured phrase as a finalized result. That remaining possibility is not evidence that this caused the user's report.

## Physical acceptance still required

On the installed Native17 P30, with background and screen off, verify and record aggregate outcome only for each case:

1. Ordinary speech without the configured phrase; isolated similar words and near-miss phrases.
2. TV/music and the assistant's own playback, including the END cue and the 300 ms suppression window after it.
3. Full configured phrase (`эй дом`) and one changed user phrase: each should produce exactly one START/session.
4. After normal completion, error, and return to wake phase: repeat negative cases and confirm there is no unsolicited START, bootstrap, or microphone upload.
5. For any unexpected activation, distinguish a finalized false phrase match from a START/session path that bypassed the detector, using only bounded state/reason counters. Do not retain or transmit background audio or recognized phrases.

The handoff records APK17 / Native17 on the P30, but this review did not verify installed source/runtime identity or perform these acoustic cases. Physical acceptance is **NOT RUN**; no claim of a device fix or reproduction is made. Existing gate tests were inspected, not rerun.

## Source identity

SHA-256:

| File | SHA-256 |
|---|---|
| `WakeDetector.java` | `494b2a5340eda65d388952054fd1ecde17672654ef94a7fe12bfa613933b6778` |
| `WakeMatchGate.java` | `5678c737bbeb475eb850d5187c61584409809e6ce118e8af497799907d54a18f` |
| `WakeMatchGateTest.java` | `3dc86adaba5967ef9062cdbbfb97a876920f89e8878a3d6e832401c68f94ff23` |
