# Direction contract: AI connections in Habab Home

Mode: Operate. Scope: a narrow settings extension inside the established Home Assistant interface. The user requested three providers, key instructions, connection checks, actual commands and voice/VAD. No redesign, new identity, illustrations, landing page, or open-ended surface exploration was requested.

## Form

Inherit Home Assistant's Roboto typography, theme colors, administrative sidebar and form vocabulary. Desktop: a compact provider selector beside the selected provider's instructions and credentials. Mobile: provider buttons followed by a single form column. Device-command and microphone controls follow the credential section, separated by a native divider. Distinguish the side-effect-free connection probe from the actual device-command action.

FORM seed: not applicable. `impeccable/reference/new-work.md`, section 3, says: “Never run the script for a local extension or a precisely specified narrow request; shape those directly.” No concept seed or fictional user approval is supplied for this existing-world extension.

## Chosen quality bar

Native Home Assistant settings quality: an administrator can select any of the three providers, find the right key console and instructions, enter a masked key, receive an accurate connection result and perform an exposed-device command. Controls and feedback must remain readable and unclipped at 1280 and 390 pixels. The microphone must stop after end-of-speech, timeout or leaving the panel. Cloud processing must be disclosed beside the action. The existing Home Assistant implementation and `habab/qa/desktop.png` are the incumbent visual reference, not a new approved comp.

## First viewport and interaction

The first desktop viewport identifies the AI settings, all three provider choices, instructions, model and key fields and primary connection action. On mobile the same content follows a clear reading order. The command block remains on the same settings page below the credentials. A real voice command supplies the distinctive interaction: detect speech, show its transcript, execute through Assist and play the generated answer.

## Constraints and evidence

Admin-only settings APIs; keys stay in local config entries and never appear in connection listings, screenshots or QA artifacts. No public-demo connection to a private Core. No regex intent parser or success simulation. OpenAI and DeepSeek are tested live; Gemini has no supplied real key. Synthetic audio and a virtual Home Assistant lamp do not establish physical hardware or iOS support.

No approved comp, image assets, comp-led state or design-system replacement applies. Screenshots: `desktop-providers.png`, `mobile-providers.png`, `desktop-voice-result.png`. Behavioral reports: `browser.json`, `acceptance.json`, `access-control.json`. Detector: `design-detector.json` is empty. New-work brand/surface lottery gates are outside this narrow extension.
