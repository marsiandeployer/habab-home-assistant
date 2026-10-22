# Home Assistant frontend demo

This interactive demo is compiled from the official Home Assistant frontend repository at commit `92599a88037ef5b7bd822efd7301028d824f7f5b`. The upstream project is licensed under Apache License 2.0; see [LICENSE.md](LICENSE.md). Habab changes: Russian demo labels and native Russian locale; subpath-safe, self-hosted assets; bounded demo-state persistence with visible API error handling; and the origin-wide service worker is disabled. The demo runs mock states only and does not connect to Home Assistant Core or devices. This build is not an official Home Assistant service or endorsement.

Additional Habab feature: a demo-only Telegram setup form. It does not transmit or persist bot tokens, and does not connect a real bot.
