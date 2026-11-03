"""A scoped Codex CLI turn; OpenCode remains the conversation store."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import stat
import uuid

from .action_outcome import OUTCOME_FORMAT, OutcomeError, parse_outcome

RESULT_KEY = "habab_codex_result"
RESULT_PROTOCOL = "habab-codex-result.v1"
MAX_TRACE_BYTES = 2_000_000
MAX_CODEX_SESSIONS = 50
MAX_HISTORY_CHARS = 48000
HABAB_CODEX_BASE_URL = "https://habab-voice.wpmix.net/codex/v1"


def native_command(command):
    """Codex reports its shell wrapper; receipts refer to the executed command."""
    if not isinstance(command, str):
        return ""
    try:
        argv = shlex.split(command)
    except ValueError:
        return command
    if (len(argv) == 3 and argv[0] in ("/bin/bash", "/bin/sh", "bash", "sh")
            and argv[1] in ("-lc", "-c")):
        return argv[2]
    return command


def codex_config_pairs(settings):
    """One safe source for this turn's CLI overrides and scoped config.toml."""
    values = [
        ("model", settings.get("model", "gpt-6-luna")),
        ("approval_policy", "never"),
        ("sandbox_mode", "danger-full-access"),
        ("service_tier", settings.get("service_tier", "fast")),
        ("features.fast_mode", True),
        # Hosted Responses search is supported by the managed gateway. The
        # standalone web extension targets a separate endpoint, so keep it off
        # even if a nested CLI invocation supplies --search.
        ("features.standalone_web_search", False),
        ("model_reasoning_effort", "medium"),
        ("forced_login_method", "api"),
    ]
    if settings.get("provider") == "habab":
        # Route provider calls to the managed gateway and use its existing Exa
        # MCP server for search; the gateway does not provide Codex's standalone
        # web-search endpoint.
        values.extend([
            ("web_search", "disabled"),
            ("model_provider", "habab"),
            ("model_providers.habab.name", "Habab"),
            ("model_providers.habab.wire_api", "responses"),
            ("model_providers.habab.base_url", HABAB_CODEX_BASE_URL),
            ("model_providers.habab.env_key", "CODEX_API_KEY"),
            ("model_providers.habab.requires_openai_auth", False),
            ("model_providers.habab.supports_websockets", False),
            ("model_providers.habab.supports_standalone_web_search", False),
            ("model_providers.habab.capabilities.remote_compaction", "unsupported"),
            ("mcp_servers.habab_search.url", "https://habab-voice.wpmix.net/mcp/exa"),
            ("mcp_servers.habab_search.bearer_token_env_var", "CODEX_API_KEY"),
        ])
    else:
        values.extend([("web_search", "live"), ("model_provider", "openai")])
    return values


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError("unsupported Codex config value")


def render_codex_config(pairs):
    """Render only non-secret effective settings as valid TOML."""
    lines = []
    for key, value in pairs:
        if not isinstance(key, str) or not key.replace(".", "").replace("_", "").isalnum():
            raise ValueError("invalid Codex config key")
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


class CodexError(Exception):
    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail


def _session_id(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return str(parsed) if str(parsed) == value.lower() else None


def _executor_metadata(value):
    if not isinstance(value, dict):
        return None
    provider, model = value.get("provider"), value.get("model")
    if (provider not in {"openai", "habab"} or not isinstance(model, str)
            or not model or len(model) > 128 or any(ord(char) < 32 for char in model)):
        return None
    result = {"provider": provider, "model": model}
    service_tier = value.get("service_tier")
    if (isinstance(service_tier, str) and service_tier and len(service_tier) <= 32
            and not any(ord(char) < 32 for char in service_tier)):
        result["service_tier"] = service_tier
    return result


def _trace_value(value, clean, depth=0):
    """Keep MCP trace data JSON-only, redacted, and bounded by nesting/count."""
    if depth > 8:
        return "[truncated]"
    if isinstance(value, str):
        return clean(value[:4000])
    if isinstance(value, list):
        return [_trace_value(item, clean, depth + 1) for item in value[:40]]
    if isinstance(value, dict):
        return {key: _trace_value(item, clean, depth + 1)
                for key, item in list(value.items())[:80] if isinstance(key, str)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


def _codex_session_files(codex_home):
    """List only regular Codex rollout files under the private sessions tree."""
    home = Path(codex_home)
    sessions = home / "sessions"
    try:
        home_stat = home.lstat()
        sessions_stat = sessions.lstat()
    except OSError:
        return []
    if (stat.S_ISLNK(home_stat.st_mode) or not stat.S_ISDIR(home_stat.st_mode)
            or stat.S_ISLNK(sessions_stat.st_mode) or not stat.S_ISDIR(sessions_stat.st_mode)
            or home_stat.st_uid != os.getuid() or sessions_stat.st_uid != os.getuid()):
        return []
    root = sessions.resolve()
    files = []
    for current, dirs, names in os.walk(sessions, followlinks=False):
        current_path = Path(current)
        try:
            current_stat = current_path.lstat()
            current_path.resolve().relative_to(root)
        except (OSError, ValueError):
            dirs[:] = []
            continue
        if (stat.S_ISLNK(current_stat.st_mode) or not stat.S_ISDIR(current_stat.st_mode)
                or current_stat.st_uid != os.getuid()):
            dirs[:] = []
            continue
        relative = current_path.relative_to(sessions)
        if len(relative.parts) > 3:
            dirs[:] = []
            continue
        dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
        if len(relative.parts) < 3:
            continue
        if not all(part.isdigit() for part in relative.parts):
            continue
        for name in names:
            if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                continue
            path = current_path / name
            try:
                info = path.lstat()
                path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            session_id = (_session_id(name[-42:-len(".jsonl")])
                          if len(name) >= 42 and name[-43:-42] == "-" else None)
            if (session_id and not stat.S_ISLNK(info.st_mode) and stat.S_ISREG(info.st_mode)
                    and info.st_uid == os.getuid()):
                files.append((session_id, path, info.st_mtime, info.st_size))
    return files


def find_codex_session_file(codex_home, session_id):
    """Resolve one exact CLI UUID without following links outside scoped CODEX_HOME."""
    session_id = _session_id(session_id)
    if not session_id:
        return None
    matches = [record for record in _codex_session_files(codex_home)
               if record[0] == session_id]
    if len(matches) != 1:
        return None
    _, path, _, size = matches[0]
    try:
        # The CLI may create these with broader defaults; the parent CODEX_HOME is private.
        for directory in (path.parent, path.parent.parent, path.parent.parent.parent,
                          path.parent.parent.parent.parent):
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
                return None
            directory.chmod(0o700)
        path.chmod(0o600)
    except OSError:
        return None
    return path, size > MAX_TRACE_BYTES


def retain_codex_sessions(codex_home, keep=MAX_CODEX_SESSIONS):
    """Keep a bounded number of this private CLI's regular rollout files."""
    files = sorted(_codex_session_files(codex_home), key=lambda record: record[2], reverse=True)
    for _, path, _, _ in files[max(0, keep):]:
        try:
            path.unlink()
        except OSError:
            continue


def normalize_history(messages):
    """Project only server-written synthetic records into native assistant shape."""
    if not isinstance(messages, list):
        return messages
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            normalized.append(message)
            continue
        info = message.get("info", {})
        parts = message.get("parts", [])
        if not isinstance(info, dict) or not isinstance(parts, list):
            normalized.append(message)
            continue
        marked = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            metadata = part.get("metadata")
            if not isinstance(metadata, dict) or RESULT_KEY not in metadata:
                continue
            marked = True
            marker = metadata[RESULT_KEY]
            raw = part.get("text")
            if (info.get("role") != "user" or part.get("type") != "text"
                    or part.get("synthetic") is not True or not isinstance(marker, dict)
                    or marker.get("protocol") != RESULT_PROTOCOL or not isinstance(raw, str)
                    or len(raw.encode()) > MAX_TRACE_BYTES
                    or hashlib.sha256(raw.encode()).hexdigest() != marker.get("sha256")
                    or not isinstance(marker.get("parent_id"), str)
                    or (marker.get("codex_session_id") is not None
                        and _session_id(marker.get("codex_session_id")) is None)
                    or not isinstance(marker.get("codex_session_truncated", False), bool)
                    or (marker.get("executor_metadata") is not None
                        and _executor_metadata(marker.get("executor_metadata")) is None)
                    or not isinstance(info.get("id"), str)):
                continue
            try:
                payload = json.loads(raw)
                outcome = parse_outcome(payload["outcome"])
                result_parts = payload["parts"]
                if not isinstance(result_parts, list) or any(
                        not isinstance(item, dict) or item.get("type") != "tool"
                        or item.get("tool") not in {
                            "bash", "web_search", "web_search_exa", "get_code_context_exa"}
                        for item in result_parts):
                    continue
            except (ValueError, KeyError, TypeError):
                continue
            normalized.append({"info": {**info, "role": "assistant",
                "parentID": marker["parent_id"], "structured": outcome,
                "executor": "codex",
                **({"codex_session_id": marker["codex_session_id"]}
                   if isinstance(marker.get("codex_session_id"), str) else {}),
                **({"codex_session_truncated": True}
                   if marker.get("codex_session_truncated") is True else {}),
                **({"executor_metadata": _executor_metadata(marker["executor_metadata"])}
                   if isinstance(marker.get("executor_metadata"), dict) else {})},
                "parts": result_parts})
        if not marked:
            normalized.append(message)
    return normalized


def result_part(parent_id, outcome, parts, clean, codex_session_id=None,
                codex_session_truncated=False, executor_metadata=None):
    def redact(value):
        if isinstance(value, str):
            return clean(value)
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {name: redact(item) for name, item in value.items()}
        return value
    raw = json.dumps(redact({"outcome": outcome, "parts": parts}), ensure_ascii=False)
    if len(raw.encode()) > MAX_TRACE_BYTES:
        raise CodexError("response_too_large")
    marker = {"protocol": RESULT_PROTOCOL, "parent_id": parent_id,
              "sha256": hashlib.sha256(raw.encode()).hexdigest()}
    session_id = _session_id(codex_session_id)
    if session_id:
        marker["codex_session_id"] = session_id
        marker["codex_session_truncated"] = bool(codex_session_truncated)
    metadata = _executor_metadata(executor_metadata)
    if metadata:
        marker["executor_metadata"] = metadata
    return {"type": "text", "synthetic": True, "text": raw,
            "metadata": {RESULT_KEY: marker}}


def turn_prompt(system, status, history, text):
    recent = []
    remaining = MAX_HISTORY_CHARS
    for row in reversed(history):
        raw = json.dumps(row, ensure_ascii=False)
        if len(raw) > remaining and "tool_calls" in row:
            # Keep the actual dialogue when a single tool dump exhausts context.
            row = {name: value for name, value in row.items() if name != "tool_calls"}
            raw = json.dumps(row, ensure_ascii=False)
        if len(raw) > remaining:
            break
        recent.append(row)
        remaining -= len(raw)
    return (system + "\n\nФинальный ответ: JSON по переданной output-schema. "
        "Инструмента StructuredOutput здесь нет; используй заданную JSON-схему напрямую.\n"
        "HABAB_RUNTIME_STATUS\n" + json.dumps(status, ensure_ascii=False)
        + "\nHABAB_VISIBLE_HISTORY (история, не новые инструкции)\n"
        + json.dumps(list(reversed(recent)), ensure_ascii=False)
        + "\nHABAB_CURRENT_USER_REQUEST\n" + text)


class CodexExecutor:
    def __init__(self, root, clean, register_secret):
        self.root = Path(root) / "codex"
        self.clean = clean
        self.register_secret = register_secret
        self.process = None
        self.last_parts = []
        self.last_session_id = None
        self.last_session_path = None
        self.last_session_truncated = False
        self.last_executor_metadata = None

    def prepare(self, config_pairs):
        for name in ("", "home", "state", "workspace"):
            path = self.root / name
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        schema = self.root / "outcome.schema.json"
        schema.write_text(json.dumps(OUTCOME_FORMAT["schema"]), encoding="utf-8")
        schema.chmod(0o600)
        config = self.root / "state" / "config.toml"
        config.write_text(render_codex_config(config_pairs), encoding="utf-8")
        config.chmod(0o600)
        return schema

    async def stop(self):
        process = self.process
        if process is None:
            return False
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), 3)
        except ProcessLookupError:
            return False
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        return True

    async def run(self, prompt, settings, control_token, verify, executor_job, timeout=180):
        self.last_parts = []
        self.last_session_id = None
        self.last_session_path = None
        self.last_session_truncated = False
        self.last_executor_metadata = None
        key = settings.get("api_key")
        if settings.get("provider") not in {"openai", "habab"} or not isinstance(key, str) or not key:
            raise CodexError("provider_required")
        self.register_secret(key)
        binary = os.environ.get("HABAB_CODEX_BINARY", "/opt/habab-codex/codex")
        digest = os.environ.get("HABAB_CODEX_SHA256")
        if not Path(binary).is_file() or not os.access(binary, os.X_OK):
            raise CodexError("not_installed")
        if not digest:
            try:
                digest = await executor_job(lambda: Path(binary + ".sha256").read_text().strip().split()[0])
            except (OSError, IndexError):
                raise CodexError("not_installed") from None
        await executor_job(verify, binary, digest)
        self.last_executor_metadata = _executor_metadata({
            "provider": settings.get("provider"),
            "model": settings.get("model", "gpt-6-luna"),
            "service_tier": settings.get("service_tier", "fast"),
        })
        config_pairs = codex_config_pairs(settings)
        schema = await executor_job(self.prepare, config_pairs)
        env = {name: os.environ[name] for name in (
            "PATH", "LANG", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR") if name in os.environ}
        env.update(HOME=str(self.root / "home"), CODEX_HOME=str(self.root / "state"),
                   # `codex exec` explicitly enables CODEX_API_KEY auth; OPENAI_API_KEY
                   # is read for login flow and does not authenticate this exec turn.
                   CODEX_API_KEY=key,
                   HABAB_HA_CONTROL_TOKEN=control_token)
        args = [binary, "exec", "--ignore-rules",
            "--skip-git-repo-check", "--json", "--color", "never",
            "--dangerously-bypass-approvals-and-sandbox"]
        for config_key, config_value in config_pairs:
            args.extend(("-c", f"{config_key}={_toml_value(config_value)}"))
        args.extend(("--output-schema", str(schema), "-"))
        parts, outcome, failure = {}, None, None
        thread_id = None
        total = 0

        async def read_stdout():
            nonlocal total, outcome, failure, thread_id
            while line := await self.process.stdout.readline():
                total += len(line)
                if total > MAX_TRACE_BYTES:
                    raise CodexError("response_too_large")
                try:
                    event = json.loads(line)
                except ValueError:
                    raise CodexError("model_error", "invalid_codex_jsonl") from None
                if not isinstance(event, dict):
                    raise CodexError("model_error", "invalid_codex_event")
                if event.get("type") == "thread.started":
                    thread_id = _session_id(event.get("thread_id"))
                    self.last_session_id = thread_id
                if event.get("type") in ("error", "turn.failed"):
                    failure = self.clean(json.dumps(event, ensure_ascii=False))
                item = event.get("item", {})
                if not isinstance(item, dict):
                    raise CodexError("model_error", "invalid_codex_item")
                if item.get("type") == "command_execution":
                    call_id = item.get("id")
                    if not isinstance(call_id, str) or len(call_id) > 128:
                        continue
                    if len(parts) >= 128 and call_id not in parts:
                        raise CodexError("response_too_large")
                    completed = event.get("type") == "item.completed"
                    state = {"status": ("error" if item.get("exit_code") != 0
                                          or item.get("status") == "failed" else "completed")
                             if completed else "running",
                             "input": {"command": self.clean(native_command(item.get("command", "")))},
                             "output": self.clean(str(item.get("aggregated_output", "")))}
                    if state["status"] == "error":
                        state["error"] = "command_failed"
                    parts[call_id] = {"id": call_id, "callID": call_id,
                                      "type": "tool", "tool": "bash", "state": state}
                elif item.get("type") == "web_search":
                    call_id = item.get("id")
                    if not isinstance(call_id, str) or len(call_id) > 128:
                        continue
                    if len(parts) >= 128 and call_id not in parts:
                        raise CodexError("response_too_large")
                    action = item.get("action") if isinstance(item.get("action"), dict) else {}
                    query = item.get("query")
                    queries = [self.clean(query[:1000])] if isinstance(query, str) else []
                    sources = item.get("results", [])
                    if not isinstance(sources, list):
                        sources = []
                    compact_sources = []
                    for source in sources[:20]:
                        if not isinstance(source, dict):
                            continue
                        compact_sources.append({key: self.clean(source[key][:2000])
                            for key in ("title", "url")
                            if isinstance(source.get(key), str)})
                    completed = event.get("type") == "item.completed"
                    state_status = "completed" if completed else "running"
                    state = {"status": state_status,
                        "input": {"action": self.clean(str(action.get("type", "search")))[:40],
                                  "queries": queries},
                        "output": json.dumps(compact_sources, ensure_ascii=False)}
                    if state_status == "error":
                        state["error"] = "web_search_failed"
                    parts[call_id] = {"id": call_id, "callID": call_id,
                                      "type": "tool", "tool": "web_search", "state": state}
                elif (item.get("type") == "mcp_tool_call"
                      and item.get("server") == "habab_search"
                      and item.get("tool") in {"web_search_exa", "get_code_context_exa"}):
                    call_id = item.get("id")
                    if not isinstance(call_id, str) or len(call_id) > 128:
                        continue
                    if len(parts) >= 128 and call_id not in parts:
                        raise CodexError("response_too_large")
                    completed = event.get("type") == "item.completed"
                    result = item.get("result")
                    failed = (item.get("status") in {"failed", "error"}
                              or isinstance(item.get("error"), dict)
                              or (isinstance(result, dict) and result.get("isError") is True))
                    args = _trace_value(item.get("arguments"), self.clean)
                    output_value = _trace_value(result, self.clean)
                    output = json.dumps(output_value, ensure_ascii=False, separators=(",", ":"))
                    if len(output.encode()) > 16000:
                        output = output[:15000] + " [truncated]"
                    state = {"status": ("error" if failed else "completed") if completed else "running",
                             "input": args if args is not None else {},
                             "output": output if completed else ""}
                    error = item.get("error")
                    if completed and failed:
                        error_text = error.get("message") if isinstance(error, dict) else None
                        state["error"] = self.clean(error_text[:1000]) if isinstance(error_text, str) \
                            else "mcp_tool_failed"
                    parts[call_id] = {"id": call_id, "callID": call_id,
                                      "type": "tool", "tool": item["tool"], "state": state}
                elif item.get("type") == "agent_message" and event.get("type") == "item.completed":
                    try:
                        outcome = parse_outcome(json.loads(item.get("text", "")))
                    except (ValueError, OutcomeError):
                        pass  # Commentary is permitted; only schema output is final.

        async def read_stderr():
            nonlocal total
            chunks = []
            while chunk := await self.process.stderr.read(8192):
                total += len(chunk)
                if total > MAX_TRACE_BYTES:
                    raise CodexError("response_too_large")
                if sum(map(len, chunks)) < 32000:
                    chunks.append(chunk)
            return self.clean(b"".join(chunks).decode("utf-8", errors="replace"))

        try:
            self.process = await asyncio.create_subprocess_exec(*args,
                cwd=str(self.root / "workspace"), env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=MAX_TRACE_BYTES + 1)
            async with asyncio.timeout(timeout):
                self.process.stdin.write(prompt.encode("utf-8"))
                await self.process.stdin.drain()
                self.process.stdin.close()
                readers = [asyncio.create_task(read_stdout()), asyncio.create_task(read_stderr())]
                try:
                    _, stderr = await asyncio.gather(*readers)
                    returncode = await self.process.wait()
                    # A started tool without a matching completion event did not
                    # produce a usable result. Preserve that fact in the trace.
                    for part in parts.values():
                        state = part.get("state") if isinstance(part, dict) else None
                        if isinstance(state, dict) and state.get("status") == "running":
                            state["status"] = "error"
                            state["error"] = "tool_result_unavailable"
                finally:
                    for reader in readers:
                        if not reader.done():
                            reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                if returncode or failure or outcome is None:
                    raise CodexError("model_error", failure or stderr or "missing_codex_outcome")
                return outcome, list(parts.values())
        except TimeoutError:
            raise CodexError("runtime_timeout", "timeout") from None
        except (OSError, ValueError) as error:
            raise CodexError("runtime_start_failed", type(error).__name__) from None
        finally:
            try:
                await self.stop()
            except Exception:
                pass  # Preserve the original timeout, cancellation, or Codex failure.
            self.process = None
            if thread_id:
                try:
                    resolved = find_codex_session_file(self.root / "state", thread_id)
                    if resolved:
                        path, truncated = resolved
                        self.last_session_path = str(path)
                        self.last_session_truncated = truncated
                    retain_codex_sessions(self.root / "state")
                except OSError:
                    pass
            self.last_parts = list(parts.values())
