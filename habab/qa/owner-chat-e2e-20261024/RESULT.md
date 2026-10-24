# Owner stand: household Telegram and shared assistant

Checked on 2026-10-24. This report distinguishes source checks, the real owner stand, and physical acceptance.

## Telegram — PASS with one live user account

- The real owner stand reports both the paired owner's DM and the household group connected. Group binding survived the candidate container restart.
- Actual Telegram flow: add bot to the private QA group → await administrator rights → promote bot → automatically finish binding without repeating the one-use nonce.
- The group is an access roster. Its members talk to the bot privately; ordinary group discussion does not execute commands. Only the owner can bind or replace the household group.
- Membership is checked before work and before delivery; leaving, removal, an unverifiable membership response, and revoked owner access fail closed. Independent source review and automated tests cover these paths.
- A live paired-owner `/help` request returned guidance for the already connected group and private conversations. An unknown or unpaired user's onboarding is covered by automated tests; no second real human account was used.
- Source now explains that the group is created in Telegram and bound using the one-use link from Habab. The final wording correction in the unconnected-group `/help` branch was tested separately and is not yet in the running v8 image.

## Shared voice history — source PASS, physical integration pending

- Fast bootstrap reads a durable owner/session/epoch context without starting OpenCode. Direct Realtime transcript writeback persists a private pending journal without generating another model response.
- Normal web history reads and home intents materialize pending turns into the existing canonical history using marked `noReply` records. Separate Telegram histories are preserved.
- Independent review found two `/new` epoch races. Both were fixed and reviewed again. Tests now cover the transition interleaving and rejection of a contextless old lease after restart using the actual saved epoch payload.
- The source helper is in owner candidate v8, but the new native v2 Realtime handler and Android client are not part of that image. No native v2 physical acceptance is claimed here.

## Model behavior — PARTIAL

- The existing chat identifies itself as Habab and no longer requires another assistant to talk to it.
- Fresh battery/status queries and the vacuum control sequence were tested through the real chat earlier. The sequence used the already connected Xiaomi integration; a new OAuth registration was not repeated. HA state transitions do not prove that a person observed movement.
- Critical v8 scenarios still reproduce an invalid claim that missing STT integrations means HA has no audio channel. They also confuse voice capabilities with observed wake recognition in part of an answer. These cases are **not accepted**, despite successful API calls.
- Read-only investigation of the pinned OpenCode source confirms that per-turn system instructions are applied. Another prompt-only retry is not evidence of a fix.
- Privacy publication must not claim full real-microphone or native v2 network acceptance from this report.

## Validation and runtime identity

- Candidate: `habab-home:owner-semantic-qa-0.3.4-20261024-v8`.
- Running image: `sha256:1132cd066394083659331f26e55450a8b2c4266df4cd10d0e4b3b13f9d6e7128`; component version `0.3.4`.
- 86 backend unit tests passed for setup actions/runtime, shared service, voice journal/context, and Telegram. The strengthened restart regression also passed separately.
- 12 Telegram transport tests and 9 browser voice lifecycle tests passed.
- New native v2 handler/client, Windows installation, and Android physical voice acceptance require their own matching build and evidence.

Evidence: `telegram-group-live-v2.json`, `telegram-current-status.json`, `telegram-owner-help-live.json`, `semantic-integrated-034-v8-critical.json`, and the scoped review notes in this directory. No credentials are included.
