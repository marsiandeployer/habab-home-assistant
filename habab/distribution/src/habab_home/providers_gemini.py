"""Native Google Gemini text transport used by the shared assistant loop."""

import asyncio
import json
import uuid
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
import httpx

from .providers import MAX_RESPONSE_BYTES, REQUEST_TIMEOUT, ProviderError, provider_api_base

MAX_TOOL_CALLS = 8
MAX_SIGNATURE_BYTES = 64 * 1024


class GeminiProvider:
    """Translate the assistant tool-loop contract to Google's native API."""

    def __init__(self, settings: dict[str, str]) -> None:
        self.provider = settings.get("provider", "gemini")
        self.api_key = settings["api_key"]
        self.model = settings["model"]
        self._calls: dict[str, tuple[str, bytes | None]] = {}

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        probe: bool = False,
    ) -> dict[str, Any]:
        contents, system_instruction = self._contents(messages)
        declarations = self._declarations(tools)
        tool_config = None
        if declarations:
            mode = "ANY" if probe else "AUTO"
            allowed = ["habab_connection_test"] if probe else None
            if probe and not any(item.name == "habab_connection_test" for item in declarations):
                raise ProviderError("tools_unsupported")
            tool_config = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
                mode=mode, allowed_function_names=allowed,
            ))

        config = types.GenerateContentConfig(
            system_instruction=system_instruction or None,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            tool_config=tool_config,
            thinking_config=self._thinking_config(),
            max_output_tokens=256 if probe else 1024,
            candidate_count=1,
        )
        client = None
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                client = await asyncio.to_thread(self._create_client)
                response = await client.aio.models.generate_content(
                    model=self.model, contents=contents, config=config,
                )
            return self._message(response)
        except ProviderError:
            raise
        except TimeoutError:
            raise ProviderError("timeout") from None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ProviderError(self._error_code(error)) from None
        finally:
            if client is not None:
                try:
                    await client.aio.aclose()
                except Exception:
                    pass

    def _create_client(self):
        """Build the SDK client away from HA's event loop."""
        options = types.HttpOptions(
            api_version="v1beta", timeout=REQUEST_TIMEOUT * 1000,
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        if self.provider == "habab":
            # Python's SDK appends api_version; OpenCode needs the full base.
            options.base_url = provider_api_base("habab").removesuffix("/v1beta")
            options.httpx_async_client = httpx.AsyncClient(follow_redirects=False)
        return genai.Client(api_key=self.api_key, http_options=options)

    def _thinking_config(self) -> types.ThinkingConfig | None:
        if self.model == "gemini-3.1-flash-lite":
            return types.ThinkingConfig(thinking_level="MINIMAL")
        if self.model == "gemini-3.8-flash":
            return types.ThinkingConfig(thinking_level="LOW")
        return None

    def _contents(self, messages: list[dict[str, Any]]) -> tuple[list[types.Content], str]:
        contents: list[types.Content] = []
        system: list[str] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            role = message.get("role")
            content = message.get("content")
            if role == "system":
                if isinstance(content, str) and content:
                    system.append(content)
                index += 1
                continue
            if role == "user":
                if not isinstance(content, str):
                    raise ProviderError("invalid_response")
                contents.append(types.Content(role="user", parts=[types.Part(text=content)]))
                index += 1
                continue
            if role == "assistant":
                parts: list[types.Part] = []
                if content is not None:
                    if not isinstance(content, str):
                        raise ProviderError("invalid_response")
                    if content:
                        parts.append(types.Part(text=content))
                calls = message.get("tool_calls") or []
                if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
                    raise ProviderError("invalid_response")
                for call in calls:
                    try:
                        call_id = call["id"]
                        function = call["function"]
                        name = function["name"]
                        args = json.loads(function["arguments"])
                    except (KeyError, TypeError, ValueError):
                        raise ProviderError("invalid_response") from None
                    if (not isinstance(call_id, str) or not call_id or len(call_id) > 128
                            or not isinstance(name, str) or not isinstance(args, dict)):
                        raise ProviderError("invalid_response")
                    prior = self._calls.get(call_id)
                    signature = prior[1] if prior and prior[0] == name else None
                    parts.append(types.Part(
                        function_call=types.FunctionCall(id=call_id, name=name, args=args),
                        thought_signature=signature,
                    ))
                    self._calls[call_id] = (name, signature)
                if parts:
                    contents.append(types.Content(role="model", parts=parts))
                index += 1
                continue
            if role == "tool":
                response_parts = []
                while index < len(messages) and messages[index].get("role") == "tool":
                    tool_message = messages[index]
                    call_id = tool_message.get("tool_call_id")
                    tool_content = tool_message.get("content")
                    if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
                        raise ProviderError("invalid_response")
                    call = self._calls.get(call_id)
                    if call is None:
                        raise ProviderError("invalid_response")
                    try:
                        result = json.loads(tool_content)
                    except (TypeError, ValueError):
                        raise ProviderError("invalid_response") from None
                    if not isinstance(result, dict):
                        raise ProviderError("invalid_response")
                    response_parts.append(types.Part(function_response=types.FunctionResponse(
                        id=call_id, name=call[0], response=result,
                    )))
                    index += 1
                contents.append(types.Content(role="user", parts=response_parts))
                continue
            raise ProviderError("invalid_response")
        if not contents:
            raise ProviderError("invalid_response")
        return contents, "\n\n".join(system)

    @staticmethod
    def _declarations(tools: list[dict[str, Any]]) -> list[types.FunctionDeclaration]:
        if not isinstance(tools, list) or len(tools) > 64:
            raise ProviderError("invalid_response")
        result = []
        names = set()
        try:
            for tool in tools:
                function = tool["function"]
                name = function["name"]
                description = function.get("description", "")
                schema = function["parameters"]
                if (not isinstance(name, str) or not name or len(name) > 128 or name in names
                        or not isinstance(description, str) or not isinstance(schema, dict)):
                    raise ProviderError("invalid_response")
                encoded_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
                if len(encoded_schema.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise ProviderError("invalid_response")
                result.append(types.FunctionDeclaration(
                    name=name, description=description, parameters_json_schema=schema,
                ))
                names.add(name)
        except (KeyError, TypeError, ValueError):
            raise ProviderError("invalid_response") from None
        return result

    def _message(self, response: Any) -> dict[str, Any]:
        try:
            candidates = response.candidates
            if not isinstance(candidates, list) or len(candidates) != 1:
                raise ProviderError("invalid_response")
            candidate = candidates[0]
            if candidate.finish_reason not in (types.FinishReason.STOP, types.FinishReason.MAX_TOKENS,
                                                types.FinishReason.FINISH_REASON_UNSPECIFIED):
                raise ProviderError("invalid_response")
            parts = candidate.content.parts
        except ProviderError:
            raise
        except (AttributeError, TypeError):
            raise ProviderError("invalid_response") from None
        text_parts = []
        calls = []
        for part in parts or []:
            text = getattr(part, "text", None)
            if text is not None and not getattr(part, "thought", False):
                if not isinstance(text, str):
                    raise ProviderError("invalid_response")
                text_parts.append(text)
            function = getattr(part, "function_call", None)
            if function is not None:
                name = getattr(function, "name", None)
                args = getattr(function, "args", None)
                call_id = getattr(function, "id", None) or str(uuid.uuid4())
                signature = getattr(part, "thought_signature", None)
                if (not isinstance(name, str) or not name or not isinstance(args, dict)
                        or not isinstance(call_id, str) or len(call_id) > 128
                        or (signature is not None and (not isinstance(signature, bytes)
                            or len(signature) > MAX_SIGNATURE_BYTES))):
                    raise ProviderError("invalid_response")
                try:
                    encoded_args = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
                except (TypeError, ValueError):
                    raise ProviderError("invalid_response") from None
                if len(encoded_args.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise ProviderError("invalid_response")
                self._calls[call_id] = (name, signature)
                calls.append({
                    "id": call_id, "type": "function",
                    "function": {"name": name, "arguments": encoded_args},
                })
        visible = "".join(text_parts)
        if len(visible.encode("utf-8")) > MAX_RESPONSE_BYTES or len(calls) > MAX_TOOL_CALLS:
            raise ProviderError("invalid_response")
        if not visible.strip() and not calls:
            raise ProviderError("invalid_response")
        return {"role": "assistant", "content": visible or None, "tool_calls": calls}

    @staticmethod
    def _error_code(error: Exception) -> str:
        code = getattr(error, "code", None)
        status = code if isinstance(code, int) else getattr(error, "status_code", None)
        if status == 400 and GeminiProvider._invalid_api_key_detail(error):
            return "invalid_auth"
        if status in (401, 403):
            return "invalid_auth"
        if status in (402, 429):
            return "quota"
        if status in (400, 404):
            return "invalid_model"
        if isinstance(error, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
            return "timeout"
        if isinstance(error, genai_errors.APIError):
            return "cannot_connect"
        return "cannot_connect"

    @staticmethod
    def _invalid_api_key_detail(error: Exception) -> bool:
        """Recognize Google's structured API_KEY_INVALID detail only."""
        details = getattr(error, "details", None)
        if not isinstance(details, dict):
            return False
        api_error = details.get("error")
        if not isinstance(api_error, dict):
            return False
        error_details = api_error.get("details")
        if not isinstance(error_details, list):
            return False
        return any(
            isinstance(detail, dict)
            and detail.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
            and detail.get("reason") == "API_KEY_INVALID"
            for detail in error_details
        )
