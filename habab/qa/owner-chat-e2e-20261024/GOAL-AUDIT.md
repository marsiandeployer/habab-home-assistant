# Telegram, web setup, and Xiaomi acceptance

The goal is not complete. The clean Xiaomi installation has reached official account verification; it still has no Xiaomi config entry or imported vacuum. Existing-device control is not evidence of fresh OAuth completion.

| Requirement | Evidence | Result |
| --- | --- | --- |
| Create and connect a bot from scratch | `fresh-bot-onboarding-v14.json`: real BotFather creation, protected UI token entry, owner nonce pairing, current native status connected | PASS |
| Configure HA from Telegram | `sun-flow-live.json`: real native entry creation and completion reply | PASS on the owner server |
| Continue a protected setup in web and return to Telegram | `met-handoff-completion-live.json`, `met-web-completed.json`, `met-loaded-readback.json` | PASS for Met; not a substitute for Xiaomi OAuth |
| Continue existing web Xiaomi setup from Telegram | `fresh-bot-xiaomi-handoff-v14.json`: actual new bot reply, same Russia OAuth flow preserved, first trusted origin attached | PASS through protected handoff |
| Preserve household member completion permissions | Native Xiaomi-to-TelegramManager regression: persist/reload member origin, reject departed member, notify only its private chat after membership is valid | PASS in native offline tests; second-human live acceptance not claimed |
| Control the existing Xiaomi vacuum | `vacuum-telegram-live.json`: fresh bot replies and independent HA states, docked → cleaning → idle → returning → docked | PASS; human-observed motion not claimed |
| Show vacuum and battery on the existing Home | `native-home-v11.json`, `public-v10-pin-mobile.json`: robot icon, native commands, battery favorite, mobile split/focus/collapse, no duplicate dashboard | PASS on the same unchanged frontend .7 |
| Fresh Xiaomi authorization, device import, and control | Current isolated setup remains at OAuth with zero Xiaomi entries | PENDING account-holder confirmation |

The v14 change passed 88 targeted native HA tests and independent source review. The existing origin is never retargeted. A first validated Telegram origin can attach to an active same-owner Xiaomi card; completed and unknown phases are excluded. No OAuth flow is restarted by that request. The generic integration controller's existing-origin policy is unchanged.

The original bot and vacuum remain on the primary server. The newly created QA bot uses a separate token and the isolated installation. Tokens, account identifiers, raw authorization URLs, and raw private replies are not included in this report.
