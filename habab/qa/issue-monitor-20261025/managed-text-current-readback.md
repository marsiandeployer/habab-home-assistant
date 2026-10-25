# Managed text relay and Core readback

**Result:** the managed text relay and selected Habab Core contract are available. This is a bounded route and one-turn readback, not physical-device acceptance.

## Relay routes

The live Nginx config from commit `d7cd9f93618cbb65b5b67604c29f7792c5ded71c` passed syntax validation and reload. The three managed text routes point to loopback port `18328`; Exa remains on `8327`, and the voice catchall remains on `8326`. Authenticated capabilities and model-list requests returned HTTP 200. The legacy voice capability and voice health requests also returned HTTP 200; the existing voice process was unchanged. The route readback made zero model calls.

The managed text process uses the source implementation from `8fe72107ceb326d435225ddf6a57453ec0dabd7d`. Its upstream text key is held in RAM; after a process restart, text readiness requires re-enrollment. Private process config and enrollment material are excluded from this report.

## Core owner overlay and one read-only reply

A six-file owner overlay from frozen commit `1b24b549083d42f2415937718200d4e86553c480` was served after one Core restart. Readback reported selected provider `habab`, effective model `habab-chat`, protocol `openai_compatible`, `text_ready=true`, and HTTP 200 for the HA root, status endpoint, AI panel, and setup-chat asset. The voice gateway process remained unchanged. Provider settings, household config, devices, and phone were not changed.

One read-only inventory question received an assistant reply. A manual comparison matched its four reported registry entries and service classifications to the mapped registry. Tool invocation metadata was unavailable, so no tool-call claim is made. A `vacuum` entity in the registry does not establish a physical vacuum device. No physical-device acceptance was performed.

## Remaining bounds

This readback does not prove general answer quality, tool execution, physical device presence, or Android runtime installation. It records one bounded reply and the route/provider metadata only.
