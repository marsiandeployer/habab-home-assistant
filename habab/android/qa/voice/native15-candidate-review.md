# Voice source candidate after native14 user diagnosis

Status: source and isolated/live-model checks only. Not an installed native15
release; the P30 still runs native14 with runtime `7119388d53035d71`.

The observed user session made one real OpenCode helper request. The helper
returned a four-entry device registry and 9.1 C weather, but the answer was not
observed in speech. A later voice session claimed to have checked without a new
helper call. The authenticated native bootstrap returned an empty Assist-exposed
snapshot despite the weather existing in Home Assistant.

The candidate clarifies the limits of that snapshot and supplies trusted selected
service/model metadata. Real Gemini Live through the public Habab relay passed
seven synthetic policy scenarios and three small-talk/session-end guard cases.
Those tests used text input and fixture function responses: they did not execute
OpenCode, Home Assistant actions, microphone capture, or physical playback. The
initial unknown-snapshot policy failure was retained in private QA before the
correction and full rerun. Reported elapsed times include harness settling.

The Android source preserves an active Gemini helper result after the 120-second
audio-upload cap, with an absolute 165-second deadline. The microphone recorder
continues to supply local frames, which the client discards during this drain;
it does not advertise listening. END waits for the canonical answer's playback.
Explicit end/cancel remains immediate, stale callbacks cannot revive a session,
and an undelivered current result is an error. This is a bounded drain of existing
work, not an unlimited conversation mode. The exact cause of the original missed
answer remains a source-supported hypothesis rather than proven event ordering.

Review caught and fixed a callback/history lock inversion and two Gemini response
boundary cases: a tool without a spoken preface, and a tool result arriving while
the preface was still being generated. Tests also cover a tool and generation
completion in the same provider frame. ASR receipts remain independent events;
the patch does not fabricate upstream turn IDs.

Validation: 70 JVM tests, zero failures/errors, one existing full-runtime archive
test skipped because no archive was supplied to this source-only run. Gradle
`:app:assembleDebug :app:lintDebug` passed. The backend realtime suite passed 29
tests in an isolated real Core image with current source mounted read-only and
network disabled. No new APK/runtime was installed or published by these checks.

Next acceptance: build a signed matching runtime/APK, verify the backend on the
cloud release, then install and repeat the actual phone voice cases, including
long helper completion, cancellation, cues, echo and screen-off operation.
