import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("prepare_wake_experiments", Path(__file__).with_name("prepare_wake_experiments.py"))
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


class PrivateWakePackTest(unittest.TestCase):
    def test_output_cannot_erase_source_or_its_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            source = parent / "pack"
            source.mkdir()
            self.pack(source)
            for destination in (source, parent, source / "child"):
                with self.assertRaisesRegex(ValueError, "overlap"):
                    producer.validate_output(source, destination)
            self.assertTrue((source / "manifest.json").is_file())

    def test_existing_unrelated_output_is_not_a_generated_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "source", root / "other"
            source.mkdir(); output.mkdir()
            self.pack(source)
            (output / "keep.txt").write_text("user data")
            with self.assertRaisesRegex(ValueError, "outside"):
                producer.validate_output(source, output)
            self.assertEqual("user data", (output / "keep.txt").read_text())

    def pack(self, root):
        entries = []
        for eid in sorted(producer.ENGINES):
            directory = root / eid
            directory.mkdir()
            names = {"classifier.tflite"} if eid == "microwakeword" else producer.ONNX_FILES
            files = []
            for name in sorted(names):
                data = (eid + name).encode()
                (directory / name).write_bytes(data)
                files.append({"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            entries.append({"engine_id": eid, "model_id": eid, "version": "test", "phrase": "эй дом",
                    "threshold": 0.97 if eid == "microwakeword" else 0.5,
                    "sliding_window": 5 if eid == "microwakeword" else 1,
                    "context_frames": 204 if eid == "microwakeword" else 16,
                    "hop_ms": {"microwakeword": 30, "openwakeword": 80, "livekit": 160}[eid],
                    "model_sha256": next(x["sha256"] for x in files if x["name"].startswith("classifier.")),
                    "frontend_sha256": "a" * 64, "files": files})
        manifest = {"schema": 1, "engines": entries}
        (root / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def test_valid_pack_has_all_three_engines_without_real_weights(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.pack(root)
            manifest, files = producer.validate_pack(root)
            self.assertEqual(3, len(manifest["engines"]))
            self.assertEqual(7, len(files))

    def test_corrupt_classifier_is_rejected_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.pack(root)
            path = root / "microwakeword/classifier.tflite"
            data = path.read_bytes()
            path.write_bytes(b"x" + data[1:])
            with self.assertRaisesRegex(ValueError, "checksum"):
                producer.validate_pack(root)

    def test_parent_path_cannot_be_a_model_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.pack(root)
            manifest["engines"][0]["files"][0]["name"] = "../classifier.onnx"
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "filenames"):
                producer.validate_pack(root)

    def test_embedding_context_is_not_decision_smoothing_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.pack(root)
            next(e for e in manifest["engines"] if e["engine_id"] == "openwakeword")["sliding_window"] = 16
            (root / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "decision window"):
                producer.validate_pack(root)


if __name__ == "__main__":
    unittest.main()
