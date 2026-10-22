"""Explicit paid-provider acceptance against an isolated localhost QA instance.

Requires a private runtime directory with qa-port.json and qa-auth.json from
onboarding, plus the QA lamp. Never use a user's working Home Assistant.
Uses OpenAI TTS to author the synthetic microphone fixture; transcription,
command understanding, actual HA actions and response audio also use live APIs.
"""

import argparse
import io
import json
from pathlib import Path
import subprocess
import threading
import time
import wave

import requests
import websocket

parser = argparse.ArgumentParser()
parser.add_argument("--runtime", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
port = json.loads((args.runtime / "qa-port.json").read_text())["port"]
token = json.loads((args.runtime / "qa-auth.json").read_text())["access_token"]
base = f"http://127.0.0.1:{int(port)}"
headers = {"Authorization": f"Bearer {token}"}
report = {"environment": "isolated localhost Home Assistant 2026.10.2", "cases": []}
ws = websocket.create_connection(f"ws://127.0.0.1:{port}/api/websocket", timeout=120)
ws.recv()
ws.send(json.dumps({"type": "auth", "access_token": token}))
assert json.loads(ws.recv())["type"] == "auth_ok"
sequence = 0


def receive_result(message_id):
    while True:
        item = json.loads(ws.recv())
        if item.get("id") == message_id and item.get("type") == "result":
            if not item["success"]:
                raise RuntimeError(item["error"]["code"])
            return item["result"]


def call(command, **data):
    global sequence
    sequence += 1
    ws.send(json.dumps({"id": sequence, "type": command, **data}))
    return receive_result(sequence)


def state(entity_id):
    response = requests.get(base + "/api/states/" + entity_id, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()["state"]


def fixture(text):
    authored_path = args.runtime / "authored-command.wav"
    if not authored_path.exists():
        key = json.loads((args.runtime / "provider-keys.json").read_text())["openai"]
        response = requests.post("https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "tts-1", "voice": "alloy", "input": text, "response_format": "wav"}, timeout=40)
        if response.status_code != 200:
            raise RuntimeError(f"fixture_synthesis_http_{response.status_code}")
        authored_path.write_bytes(response.content)
    with wave.open(str(authored_path)) as audio:
        sample_rate = audio.getframerate()
        samples = audio.readframes(audio.getnframes())
    converted = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sample_rate),
        "-ac", "1", "-i", "pipe:0", "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
    ], input=samples, check=True, capture_output=True).stdout
    pcm = b"\0" * 16000 + converted + b"\0" * 96000
    output = args.output.parent / "microphone-fixture.wav"
    with wave.open(str(output), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(pcm)
    return pcm


def record(case):
    report["cases"].append(case)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(case, ensure_ascii=False), flush=True)


def redact(value):
    """Do not retain the ephemeral TTS streaming token in evidence."""
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items() if key not in ("token", "access_token", "refresh_token")}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


args.output.parent.mkdir(parents=True, exist_ok=True)
try:
    connections = call("habab_home/providers/list")["connections"]
    speech = next(item for item in connections if item["provider"] == "openai")
    call("homeassistant/expose_new_entities/set", assistant="conversation", expose_new=False)
    call("homeassistant/expose_entity", assistants=["conversation"], entity_ids=["light.testovaia_lampa"], should_expose=True)
    call("homeassistant/expose_entity", assistants=["conversation"], entity_ids=["input_boolean.hidden_power", "input_boolean.test_light_power"], should_expose=False)
    for entry in connections:
        provider = entry["provider"]
        if provider not in ("openai", "deepseek"):
            continue
        for text, expected in [("Включи тестовую лампу", "on"), ("Выключи тестовую лампу", "off")]:
            result = call("conversation/process", text=text, language="ru", agent_id=entry["entities"]["conversation"])
            actual = state("light.testovaia_lampa")
            record({"provider": provider, "kind": "text_action", "command": text,
                    "expected": expected, "state": actual, "response": result,
                    "pass": actual == expected and result["response"]["response_type"] == "action_done"})
        for text in ["Включи закрытое устройство", "Включи лампу на Марсе", "Открой замок входной двери"]:
            requests.post(base + "/api/services/light/turn_off", headers=headers,
                json={"entity_id": "light.testovaia_lampa"}, timeout=15).raise_for_status()
            result = call("conversation/process", text=text, language="ru", agent_id=entry["entities"]["conversation"])
            record({"provider": provider, "kind": "unexposed_or_absent_device", "command": text,
                    "response": result, "pass": state("input_boolean.hidden_power") == "off" and state("light.testovaia_lampa") == "off"})

        existing = call("assist_pipeline/pipeline/list")["pipelines"]
        old = next((item for item in existing if item["conversation_engine"] == entry["entities"]["conversation"]), None)
        params = {"name": "Habab " + provider, "language": "ru", "conversation_engine": entry["entities"]["conversation"],
                  "conversation_language": "ru", "prefer_local_intents": False,
                  "stt_engine": speech["entities"]["stt"], "stt_language": "ru-RU",
                  "tts_engine": speech["entities"]["tts"], "tts_language": "ru-RU", "tts_voice": None,
                  "wake_word_entity": None, "wake_word_id": None}
        pipeline = call("assist_pipeline/pipeline/update", pipeline_id=old["id"], **params) if old else call("assist_pipeline/pipeline/create", **params)
        if not isinstance(pipeline, dict) or "id" not in pipeline:
            pipeline = next(item for item in call("assist_pipeline/pipeline/list")["pipelines"] if item["conversation_engine"] == entry["entities"]["conversation"])
        pcm = fixture("Включи тестовую лампу")
        sequence += 1
        run_id = sequence
        ws.send(json.dumps({"id": run_id, "type": "assist_pipeline/run", "pipeline": pipeline["id"],
                            "start_stage": "stt", "end_stage": "tts", "input": {"sample_rate": 16000}}))
        receive_result(run_id)
        events = []
        handler = None
        stopped = threading.Event()
        sender = None

        def send_audio():
            for index in range(0, len(pcm), 640):
                if stopped.is_set():
                    return
                ws.send_binary(bytes([handler]) + pcm[index:index + 640])
                time.sleep(0.02)
            # The server should have detected the end. EOS is a bounded fallback
            # for a recording with no speech; it is not accepted as VAD proof.
            if not stopped.is_set():
                ws.send_binary(bytes([handler]))

        while True:
            event = json.loads(ws.recv())
            if event.get("id") != run_id or event.get("type") != "event":
                continue
            event = event["event"]
            events.append(event)
            print(json.dumps({"provider": provider, "stage": event["type"]}), flush=True)
            if event["type"] == "run-start":
                handler = event["data"]["runner_data"]["stt_binary_handler_id"]
            elif event["type"] == "stt-start":
                sender = threading.Thread(target=send_audio, daemon=True)
                sender.start()
            elif event["type"] in ("stt-vad-end", "stt-end"):
                stopped.set()
            elif event["type"] in ("run-end", "error"):
                stopped.set()
                break
        if sender:
            sender.join(timeout=2)
        types = [event["type"] for event in events]
        transcript = next((event["data"]["stt_output"]["text"] for event in events if event["type"] == "stt-end"), None)
        audio_result = next((event["data"]["tts_output"] for event in events if event["type"] == "tts-end"), None)
        audio_bytes = 0
        if audio_result and audio_result["url"].startswith("/api/"):
            response = requests.get(base + audio_result["url"], headers=headers, timeout=30)
            if response.status_code == 200:
                audio_bytes = len(response.content)
                (args.output.parent / f"{provider}-response.mp3").write_bytes(response.content)
        actual = state("light.testovaia_lampa")
        record({"provider": provider, "kind": "voice_pipeline", "synthetic_input": True,
                "transcript": transcript, "events": redact(events), "state": actual, "response_audio_bytes": audio_bytes,
                "pass": all(item in types for item in ["stt-vad-start", "stt-vad-end", "stt-end", "intent-end", "tts-end", "run-end"]) and "error" not in types and actual == "on" and audio_bytes > 1000})
        requests.post(base + "/api/services/light/turn_off", headers=headers, json={"entity_id": "light.testovaia_lampa"}, timeout=15).raise_for_status()
    report["passed"] = all(case["pass"] for case in report["cases"])
finally:
    ws.close()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
if not report.get("passed"):
    raise SystemExit("Live voice acceptance failed; inspect the saved report")
