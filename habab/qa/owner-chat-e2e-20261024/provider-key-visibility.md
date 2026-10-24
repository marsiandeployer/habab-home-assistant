# Saved provider keys: admin settings

Verified on 2026-10-24 in authenticated native `/habab-ai` on owner-test, image `habab-home:owner-integrated-qa-0.3.4-20261024-v15`.

- DeepSeek and Gemini: displayed value equals the selected entry's saved key; default field type is `text`.
- Eye button hides (`password`) and reveals (`text`) the value, with a localized accessible label.
- Typing while a key request is pending preserves the draft; a late response cannot overwrite it.
- Disconnect clears the field and invalidates pending responses.
- The new command is registered with `require_admin`; 13 offline unit tests pass, including rejection of anonymous/non-admin callers before credential lookup and rejection of missing/foreign/empty entries.
- JavaScript syntax, diff whitespace, Gitleaks and exact private-credential scans pass.

No provider keys were changed or sent to a model during this check. Screenshot inspection used a hidden field. Windows and physical Android installation of this change were not tested.
