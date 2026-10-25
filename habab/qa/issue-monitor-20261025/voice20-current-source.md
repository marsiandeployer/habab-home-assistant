# Issue #20: current-source review

**Result:** one narrow late-event race was confirmed and fixed in `RealtimeVoice`; the model's semantic end policy and END-cue path already exist in Native17. No keyword classifier or prompt change was made. A passive monotonic timing hook now marks the semantic-end and local finish path without changing end behavior. No physical goodbye trace was available or collected, so the reported ~20-second delay remains unlocalized.

## Confirmed and fixed

`finish()` sets `ending = true`, cancels active interactions, interrupts playback, then clears/cancels the provider socket. Before this change, `send()` allowed an unscoped send whenever the socket still existed; only sends with an `expectedEpoch` checked `ending`. A tool worker could therefore finish its `credentials.call("turn", ...)` in the small interval after close began but before the socket was cleared, and enqueue the old `function_call_output` during teardown. The send gate now checks the volatile ending state while holding `connectionLock`, including for unscoped late tool output. Once `ending` is set, a send cannot pass the gate; after the socket is cleared, it cannot pass the socket check either.

The semantic `end_voice_session` event is still accepted only for the current response epoch and an empty argument object. It calls `finish()` directly. The model policy in `realtime.py` already asks for this tool on explicit farewells or standalone final thanks, while excluding follow-up questions, quoted phrases, negation, and non-farewell uses of «пока». No source evidence supports replacing that model decision with local substring matching.

## Remaining latency boundary

On a normal end, `RealtimeVoice.finish()` stops playback/provider transport before `history.finish(listener::ended, ...)`, but `listener::ended` is queued behind accepted transcript writes. `VoiceService` requests END only after that callback reaches the capture loop. `VoiceHistoryQueue.finish()` also starts an independent 1,500 ms timer: if accepted writes have not drained by then, it invokes the failure callback and cancels the worker. Thus a slow successful transcript write can delay the normal END callback only until the timer wins; after that the bounded path is an error, not a successful END. Scheduler delay is outside that bound. This is a source-level local wait, but there is no trace connecting it to the user's reported ~20 seconds; do not attribute that observation to this queue without measurement.

Moving the END callback ahead of the journal drain would currently let `VoiceService.endSession()` call `RealtimeVoice.close()`, which closes the history queue and can discard accepted writes. That requires coordinated lifecycle ownership of `VoiceService` and `VoiceHistoryQueue`, outside this issue assignment's edit scope. No such change was made; the queue's existing 1,500 ms failure bound is retained.

## Bounded timing hook

`RealtimeVoice.timing()` records fixed-name millisecond values in the existing `timingsMs` status map and `HababVoiceTiming` log. The end markers are `end_tool_event_received_elapsed_ms` and `end_tool_accepted_elapsed_ms` (absolute `SystemClock.elapsedRealtime()` values), `end_tool_dispatch_ms` (event-to-acceptance duration), then `end_finish_enter_ms`, `end_playback_stopped_ms` or `end_playback_stop_failed_ms`, `end_socket_cancel_requested_ms`, and `end_history_drained_ms` or `end_history_failed_ms`, followed by `end_listener_callback_ms`. The latter durations are measured from acceptance.

`VoiceService` adds `lastCueElapsedRealtimeAt` and `lastCueCompletedElapsedRealtimeAt` beside the existing `lastCue` status. They use the same monotonic clock; `-1` means the cue has not completed yet. Together these fields let a later status readback measure end-event-to-END-cue request and completion without retaining audio or transcript text.

## Evidence and verification

- Native17 handoff says the end policy/cues are present in the installed APK/runtime, but fresh acoustic acceptance is absent: `habab/android/qa/handoff-20261025-android-to-web.md`.
- Existing text-only Gemini receipt checks explicit-goodbye tool selection, but it does not test P30 microphone timing, playback, or END: `habab/android/qa/voice/native15-policy-smalltalk-end-guards.json`.
- The send-gate regression test rejects late unscoped sends while ending and preserves current-epoch sends during an active session; its earlier focused run passed.
- Scoped Java compilation passed: `timeout 120s gradle --no-daemon -p habab/android :app:compileDebugJavaWithJavac`.
- No APK assembly, phone, audio, model, or server process was exercised.

## Next physical measurement

After normal P30 unlock, use one explicit «Пока» turn and read only the `timingsMs` end markers plus `lastCueElapsedRealtimeAt` and `lastCueCompletedElapsedRealtimeAt` from `voice-status.json`. Check the cue is `end`, `lastCueCompleted` is true, and the END-to-cue intervals are plausible. Do not retain audio or transcript text. The current `native15-policy-smalltalk-end-guards.json` remains useful for the text-only semantic case; it does not replace this physical measurement. A held-open HA tool and post-END upload check remain separate follow-up coverage.
