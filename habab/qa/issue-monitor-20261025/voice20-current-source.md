# Issue #20: current-source review

**Result:** one narrow late-event race was confirmed and fixed in `RealtimeVoice`; the model's semantic end policy and END-cue path already exist in Native17. No keyword classifier or prompt change was made. The remaining end-to-cue delay still includes the transcript journal drain, which cannot be separated safely within the owned Java file alone. No physical goodbye trace was available or collected.

## Confirmed and fixed

`finish()` sets `ending = true`, cancels active interactions, interrupts playback, then clears/cancels the provider socket. Before this change, `send()` allowed an unscoped send whenever the socket still existed; only sends with an `expectedEpoch` checked `ending`. A tool worker could therefore finish its `credentials.call("turn", ...)` in the small interval after close began but before the socket was cleared, and enqueue the old `function_call_output` during teardown. The send gate now checks the volatile ending state while holding `connectionLock`, including for unscoped late tool output. Once `ending` is set, a send cannot pass the gate; after the socket is cleared, it cannot pass the socket check either.

The semantic `end_voice_session` event is still accepted only for the current response epoch and an empty argument object. It calls `finish()` directly. The model policy in `realtime.py` already asks for this tool on explicit farewells or standalone final thanks, while excluding follow-up questions, quoted phrases, negation, and non-farewell uses of «пока». No source evidence supports replacing that model decision with local substring matching.

## Remaining latency boundary

On a normal end, `RealtimeVoice.finish()` stops playback/provider transport before `history.finish(listener::ended, ...)`, but `listener::ended` is queued behind accepted transcript writes. `VoiceService` requests END only after that callback reaches the capture loop. `VoiceHistoryQueue.finish()` also starts an independent 1,500 ms timer: if accepted writes have not drained by then, it invokes the failure callback and cancels the worker. Thus a slow successful transcript write can delay the normal END callback only until the timer wins; after that the bounded path is an error, not a successful END. Scheduler delay is outside that bound. This is a source-level local wait, but there is no trace connecting it to the user's reported ~20 seconds; do not attribute that observation to this queue without measurement.

Moving the END callback ahead of the journal drain would currently let `VoiceService.endSession()` call `RealtimeVoice.close()`, which closes the history queue and can discard accepted writes. That requires coordinated lifecycle ownership of `VoiceService` and `VoiceHistoryQueue`, outside this issue assignment's edit scope. No such change was made; the queue's existing 1,500 ms failure bound is retained.

## Evidence and verification

- Native17 handoff says the end policy/cues are present in the installed APK/runtime, but fresh acoustic acceptance is absent: `habab/android/qa/handoff-20261025-android-to-web.md`.
- Existing text-only Gemini receipt checks explicit-goodbye tool selection, but it does not test P30 microphone timing, playback, or END: `habab/android/qa/voice/native15-policy-smalltalk-end-guards.json`.
- Added a regression test for the send gate: late unscoped sends are rejected while ending; current-epoch sends remain allowed for an active session.
- Focused verification passed: `timeout 120s gradle --no-daemon -p habab/android :app:testDebugUnitTest --tests ru.habab.home.RealtimeVoiceSendGateTest`.
- `git diff --check` passed. No APK assembly, phone, audio, model, or server process was exercised.

## Next physical measurement

On the installed Native17 P30, capture a privacy-safe monotonic timing trace for one explicit «пока» and one standalone final «спасибо»: speech-stop/VAD, `end_voice_session` receipt, `finish()` entry, audio upload stop, socket cancel, transcript-drain completion or failure, END cue request/play completion, and return to wake. Do not retain audio or full transcript. Repeat with an active HA tool held open when the operator can safely provide that scenario; confirm its late result is not sent after `ending` and that no post-END audio is uploaded. This is required to distinguish semantic selection delay from local journal/cue delay and to complete the issue's P30 acceptance.
