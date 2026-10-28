"""Gemini Live bootstrap. Only a single-use constrained token reaches the client."""
from datetime import datetime, timedelta, timezone

from aiohttp import ClientTimeout

DEFAULT_MODEL = "gemini-3.8-live"
MODELS = {DEFAULT_MODEL, "gemini-3.1-flash-live-preview"}
TOKEN_URL = "https://generativelanguage.googleapis.com/v1beta/auth_tokens"
WEBSOCKET_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained"
)


def live_setup(model, conversation):
    """Translate our shared voice policy, not an OpenAI wire session."""
    if not isinstance(model, str) or model not in MODELS:
        raise ValueError("unsupported_realtime_model")
    declarations = [{
        "name": tool["name"], "description": tool["description"],
        "parametersJsonSchema": tool["parameters"],
        # Keep the conversation responsive while the shared assistant works.
        "behavior": "NON_BLOCKING" if tool["name"] == "ask_home_assistant" else "BLOCKING",
    } for tool in conversation["tools"]]
    return {
        "model": "models/" + model,
        "generationConfig": {"responseModalities": ["AUDIO"], "maxOutputTokens": 1024},
        "systemInstruction": {"parts": [{"text": conversation["instructions"]}]},
        "tools": [{"functionDeclarations": declarations}],
        "inputAudioTranscription": {"languageCodes": ["ru-RU"]},
        "outputAudioTranscription": {},
        "realtimeInputConfig": {
            "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
            "automaticActivityDetection": {"disabled": False, "prefixPaddingMs": 200,
                                           "silenceDurationMs": 650},
        },
    }


async def create_connection(client, api_key, model, conversation):
    setup = live_setup(model, conversation)
    now = datetime.now(timezone.utc)
    body = {
        "uses": 1,
        "expireTime": (now + timedelta(seconds=180)).isoformat(),
        "newSessionExpireTime": (now + timedelta(seconds=60)).isoformat(),
        # Empty fieldMask means the entire setup is locked by this server request.
        "bidiGenerateContentSetup": setup,
    }
    async with client.post(
        TOKEN_URL, headers={"x-goog-api-key": api_key}, json=body,
        timeout=ClientTimeout(total=25), allow_redirects=False,
    ) as response:
        if response.status != 200:
            raise ValueError("realtime_provider_rejected")
        token = await response.json()
    if not isinstance(token.get("name"), str) or not token["name"]:
        raise ValueError("invalid_realtime_response")
    return {"provider": "gemini", "value": token["name"],
            "expires_at": token.get("expireTime"), "model": model,
            "websocket_url": WEBSOCKET_URL, "setup": setup}
