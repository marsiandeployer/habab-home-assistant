# Telegram transport worker

The Python backend owns pairing, authorization, deduplication, and agent context. This worker only translates Telegram Bot API updates and outbound send/typing requests over newline-delimited JSON on stdin/stdout. It starts no public listener and does not receive or construct agent prompts.

Run `node /opt/habab-telegram/worker.mjs`. Send one JSON object per line:

- First: `{"type":"configure","token":"..."}`. The token travels only on stdin; it is never emitted or logged.
- Updates: `{type:"ready",username}`, `{type:"message",chat_id,user_id,message_id,chat_type,text}`, and bounded `{type:"error",operation,code,fatal,request_id?}` events on stdout. `operation` is `configure`, `send`, `typing`, or `poll`; `fatal` indicates whether the Python bridge should stop this worker. Text such as `/start nonce`, `/new`, `/cancel`, and `/help` is passed through unchanged.
- Commands: `{type:"send",chat_id,text,request_id?}`, `{type:"typing",chat_id}`, and `{type:"shutdown"}`.
- Responses: `{type:"sent",request_id?}` only after all chunks were accepted by Telegram, or `{type:"error",operation:"send",request_id,code,fatal:false}` when sending fails. Typing failures are nonfatal; configure failures and polling HTTP 409 conflicts are fatal. Error codes contain no raw Bot API response or token.

Text is split at safe Unicode boundaries into chunks of at most 3,800 UTF-16 code units. Outbound Bot API work is serialized; network errors and HTTP 429 are retried at most once, with 429 waits capped at three seconds. Polling uses `getUpdates` long polling with `allowed_updates: ["message"]`.

Install dependencies during image build from this directory (`npm ci`). There are no external packages. `npm test` runs the transport unit tests. See [PROVENANCE.md](PROVENANCE.md) for the local vendor source record and licensing caveat.
