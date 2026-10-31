#!/usr/bin/env python3
"""Verify an operator-provided local model pack and stage it in ignored APK assets.

No model download, user audio, credentials, or redistribution rights are implied.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[1]
ENGINES = {"microwakeword", "openwakeword", "livekit"}
ONNX_FILES = {"classifier.onnx", "melspectrogram.onnx", "embedding_model.onnx"}


def validate_pack(pack):
    pack = Path(pack).resolve()
    manifest_path = pack / "manifest.json"
    if manifest_path.stat().st_size > 128 * 1024:
        raise ValueError("Model manifest is too large")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported model pack schema")
    entries = manifest.get("engines", [])
    if len(entries) != 3 or {e.get("engine_id") for e in entries} != ENGINES:
        raise ValueError("Expected all three experimental engines exactly once")
    verified, total = [], 0
    for engine in entries:
        eid = engine["engine_id"]
        expected = {"classifier.tflite"} if eid == "microwakeword" else ONNX_FILES
        files = engine.get("files", [])
        if len(files) != len(expected) or {f.get("name") for f in files} != expected:
            raise ValueError("Unexpected model pack filenames")
        if engine.get("phrase") != "эй дом" or not 0 <= engine["threshold"] <= 1:
            raise ValueError("Unsupported experimental phrase or threshold")
        if engine["sliding_window"] != (5 if eid == "microwakeword" else 1):
            raise ValueError("Unexpected decision window")
        if engine["context_frames"] != (204 if eid == "microwakeword" else 16):
            raise ValueError("Unexpected model context")
        if engine["hop_ms"] != {"microwakeword": 30, "openwakeword": 80, "livekit": 160}[eid]:
            raise ValueError("Unexpected model cadence")
        for item in files:
            source = pack / eid / item["name"]
            if source.is_symlink() or not source.resolve().is_relative_to(pack):
                raise ValueError("Model file escapes the pack")
            size = source.stat().st_size
            if size != item["bytes"] or not 0 < size <= 16 * 1024 * 1024:
                raise ValueError("Model file size mismatch")
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            if sha != item["sha256"]:
                raise ValueError("Model file checksum mismatch")
            if item["name"].startswith("classifier.") and sha != engine["model_sha256"]:
                raise ValueError("Classifier identity mismatch")
            total += size
            verified.append((source, eid + "/" + item["name"]))
    if total > 16 * 1024 * 1024:
        raise ValueError("Model pack exceeds 16 MiB")
    return manifest, verified


def validate_output(pack, output):
    pack, output = Path(pack).resolve(), Path(output).resolve()
    if pack.is_relative_to(output) or output.is_relative_to(pack):
        raise ValueError("Output must not overlap the source model pack")
    if output.exists() and any(output.iterdir()):
        manifest = output / "manifest.json"
        if not manifest.is_file() or {p.name for p in output.iterdir()} - ENGINES - {"manifest.json"}:
            raise ValueError("Output contains files outside a generated model bundle")
        # Verify the old generated bundle before allowing its removal.
        _, old_files = validate_pack(output)
        allowed = {relative for _, relative in old_files} | {"manifest.json"}
        if any(p.is_symlink() or (p.is_file() and p.relative_to(output).as_posix() not in allowed)
               for p in output.rglob("*")):
            raise ValueError("Output contains files outside a generated model bundle")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "app/src/main/assets/voice/experiments")
    args = parser.parse_args()
    if args.output.is_symlink():
        raise ValueError("Output must not be a symlink")
    output = validate_output(args.pack_dir, args.output)
    manifest, files = validate_pack(args.pack_dir)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wake-model-build-", dir=output.parent) as temporary:
        stage = Path(temporary) / "experiments"
        stage.mkdir()
        for source, relative in files:
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        if output.exists():
            shutil.rmtree(output)
        stage.replace(output)
    print(json.dumps({"engines": sorted(ENGINES), "files": len(files), "output": str(output)}))


if __name__ == "__main__":
    main()
