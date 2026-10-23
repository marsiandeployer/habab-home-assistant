# Local vendor provenance

`vendor/noxon-telegram` is a deliberately small local package adapted from selected provider-neutral Telegram message transport code in `/root/aisell/botplatform/src/bot.ts` at commit `d4a84aea91f15a2c81b875803bcbe9ce0d8c7e0d`.

- The source `splitMessage` helper is at `botplatform/src/bot.ts:10659-10664` in that commit. Its direct fixed-width slicing behavior was retained as the starting point and changed to preserve Unicode code points, honor Telegram's UTF-16 text limit with a 3,800-unit cap, and prefer whitespace boundaries.
- Transport behavior is newly implemented for this package using the Telegram Bot API and Node built-ins. No AISell application, provider, task, history, account, credential, payment, or infrastructure logic is included.
- The source package metadata declares `ISC`, but no corresponding license text was present alongside the selected source. This provenance note records that metadata only; it makes no authorship or copyright assertion and does not recreate a license file. Keep this limitation visible for human licensing review before redistribution.
- The local vendor package has no third-party runtime dependencies. `package-lock.json` pins the file dependency and records no external packages.
