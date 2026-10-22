# Direction contract: setup chat

Mode: Operate. A narrow extension of the Home Assistant dashboard. The user's screenshot places a chat icon in the upper toolbar, immediately before the plus icon. The icon opens a minimal ChatGPT-style conversation, using the existing BUT private OpenCode architecture as a functional reference. No redesign or OpenCode editor UI is requested.

Inherit Home Assistant theme colors and Roboto. Keep the dashboard outside the conversation unchanged. Desktop uses a centered chat dialog; mobile uses the full viewport. The header has a title, new-chat action and close button. Messages, one composer, send and stop are the working surface. Markdown answers use HA's sanitized `ha-markdown` component when available, with plain-text fallback.

FORM seed: not applicable under new-work section 3's narrow-extension exception. No concept seed, approved comp or new visual identity. The supplied screenshot is the placement reference, stored as `request.png`. BUT is a read-only architecture/reference source; its application and production are unchanged.

Chosen quality bar: the icon is visible in the requested toolbar location, the chat is readable at 1280x800 and 390x844, controls support keyboard focus and Escape, errors give a recovery action, and the conversation preserves history. Root execution requires a native HA administrator. The public demo has an editable composer and real DeepSeek replies, with a disclosure that real devices are connected in the home installation. It has no tools or private-Core access. The user's report that the input was disabled is a material regression requirement: typing and sending must be verified in the public browser, not only by assigning a value in JavaScript.

Native Docker acceptance uses real DeepSeek/OpenCode and verifies a written file directly in the container. Separate tests cover non-admin denial, another admin's session, concurrent requests and cancellation of an actual bash process. No physical Bluetooth success is claimed. Ordinary extension: preserve incumbent design-system files.
