# Voice assistant acceptance — 22 October 2026

Implemented in the local Docker distribution, not connected to the public demo.

- Providers: OpenAI, DeepSeek, Google Gemini; administrator-only connection panel, instructions, masked credentials, model/tool-calling probe and native config entries.
- Native Assist agents execute exposed-device actions. OpenAI STT/TTS and Home Assistant VAD complete the microphone pipeline; DeepSeek can be selected for command understanding.
- Real API acceptance: **12/12 PASS** for OpenAI and DeepSeek, including on/off, a hidden device, absent targets and audio → VAD → STT → action → TTS with state readback. See `acceptance.json`.
- Browser: **PASS** at 1280×800 and 390×844, all three provider guides, invalid-key handling, check of saved credentials, clearing the field and DeepSeek microphone execution. No JavaScript errors; expected `invalid_auth` is the deliberate negative check. See `browser.json`.
- Access control: **4/4 PASS**, including non-admin denial for list/save/check and anonymous denial for status. See `access-control.json`.
- Protocol/failure tests: **7 Python tests PASS** in the pinned image. Voice lifecycle: **3 Node tests PASS**. Existing Flask state suite: **7 tests and 4 subtests PASS**.
- The running QA source and built image match all **17 component source files**. Image identity and file hashes are in `build-identity.json`; tag `habab-home:voice-20261022`.
- Independent finish review: **ship** after its missing direction-contract input was supplied. The verdict pass scored that specific input finding resolved; the review found no material source/UI defect. Independent documentation check: **ship**, existing HA system preserved.
- Secret scan: supplied and sourced API key values absent from implementation, documentation, screenshots and QA artifacts. Streaming/audio authorization tokens were removed from saved traces. Native configuration uses Home Assistant's standard local storage, not newly added encryption.

An early failed run is retained in `acceptance-first-failed.json`: robot speech was transcribed incorrectly, and DeepSeek substituted an exposed lamp for an unknown target. The generic target policy was tightened and the input fixture replaced with clear synthetic speech. Final runs passed; this is not a guarantee of perfect natural-language interpretation.

The QA fixture enables the native recorder component so the stock frontend's `recorder/info` bootstrap is present. No recorder behavior was added to the distribution as part of this task.

Limits: Gemini live API was not tested because no Gemini key was supplied. Audio was synthetic and devices were virtual entities in an isolated Home Assistant 2026.10.2. Physical microphones, Zigbee devices, iOS and wake-word operation were not tested. The public demo was not used to access a private home. No production instance was restarted or modified for these tests.

Temporary QA runtime, authentication tokens and copied provider keys are removed after verification. The Docker image, source, key-free reports, screenshots and synthetic audio remain reviewable in the workspace. Setup instructions and upstream protocol examples: `habab/docs/VOICE.md`.
