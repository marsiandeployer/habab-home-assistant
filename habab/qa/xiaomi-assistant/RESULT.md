# Xiaomi Home through the built-in assistant

PASS on Home Assistant 2026.10.2, in an isolated Linux Docker installation using real DeepSeek/OpenCode and Xiaomi OAuth. The image and source hashes are recorded in build-identity.json.

The browser started without the Xiaomi custom component and connected one real Robot Vacuum S10 through the chat card: request → pinned installation → approved Core restart → Xiaomi consent → native config entry loaded. The registry contains one Xiaomi device and 130 entities; the vacuum state was docked. No physical control commands were sent.

The test forced homeassistant.local resolution to fail. The owner completed sign-in using the card's protected callback field without editing Windows hosts. Callback codes and supplied Xiaomi credentials were absent from model message/tool parts and the wizard Store. Screenshots show native cards cropped from the actual browser.

Ten live access checks passed for a read-only user and a separate administrator; their temporary accounts were deleted. Owner records and callback state are bound to the initiating user. An administrator with permitted root commands can access the container configuration; this is not a sandbox between administrators.

The original request was exercised in the full browser flow. Four additional model requests were reviewed against actual tool traces: negative/no changes, failed legacy login, another brand, and a fresh Mi Home vacuum request. A first semantic fixture accidentally retained an inactive Xiaomi entry; it was removed and all four cases were rerun. The Tuya risk case verifies brand selection and absence of Xiaomi mutations; it does not validate Tuya setup or advice.

The browser reported an existing recorder/info unknown_command from the minimal distribution, rendered as Object by the frontend. Xiaomi setup completed; this report does not claim the entire HA page has no errors. Physical Windows Docker Desktop/WSL was not exercised.

The owner-test route was restored on explicit user instruction, with the existing account, HA authentication and the new assistant image. Its shutdown timer was cancelled. The anonymous demo remains separate from the private Xiaomi QA installation.

Fixed defects: missing webhook dependency caused native OAuth callback 404; device_registry.async_get_devices without filters returned no devices; current native DeviceEntry uses config_entry_id. Tests exercise the native registry collection rather than a permissive mapping stub.

Validation: 37 native integration tests, JavaScript syntax, full browser flow, four semantic cases, ten live ACL cases, running-source identity and credential boundary checks. Package hashes and publication checks are recorded separately. Personal identifiers, account data, credentials and raw model transcripts are excluded.
