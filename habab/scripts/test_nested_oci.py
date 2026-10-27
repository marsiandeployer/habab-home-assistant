#!/usr/bin/env python3
"""Focused regression tests for nested OCI indexes in Buildx archives."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
_SPEC = importlib.util.spec_from_file_location(
    "release_candidate", Path(__file__).resolve().with_name("release-candidate.py")
)
assert _SPEC is not None and _SPEC.loader is not None
release_candidate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(release_candidate)


def blob(raw: bytes) -> tuple[str, bytes]:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}", raw


def descriptor(digest: str, raw: bytes, **metadata: object) -> dict:
    return {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": digest,
            "size": len(raw), **metadata}


def make_oci(path: Path, corrupt_nested_size: bool = False, nested: bool = True) -> None:
    files: dict[str, bytes] = {
        "oci-layout": json.dumps({"imageLayoutVersion": "1.0.0"}).encode(),
    }
    leaf_descriptors = []
    for architecture in ("amd64", "arm64"):
        layer_digest, layer = blob(f"layer-{architecture}".encode())
        config = {
            "os": "linux",
            "architecture": architecture,
            "config": {"Labels": {
                "org.opencontainers.image.revision": "e" * 40,
                "io.habab.source.dirty": "false",
            }},
        }
        config_raw = json.dumps(config, sort_keys=True).encode()
        config_digest, _ = blob(config_raw)
        files[f"blobs/sha256/{config_digest.split(':')[1]}"] = config_raw
        files[f"blobs/sha256/{layer_digest.split(':')[1]}"] = layer
        manifest = {
            "schemaVersion": 2,
            "config": {"digest": config_digest, "size": len(config_raw)},
            "layers": [{"digest": layer_digest, "size": len(layer)}],
        }
        manifest_raw = json.dumps(manifest, sort_keys=True).encode()
        manifest_digest, _ = blob(manifest_raw)
        files[f"blobs/sha256/{manifest_digest.split(':')[1]}"] = manifest_raw
        leaf_descriptors.append({
            "digest": manifest_digest,
            "size": len(manifest_raw),
            "platform": {"os": "linux", "architecture": architecture},
        })

    nested_raw = json.dumps({"schemaVersion": 2, "manifests": leaf_descriptors}, sort_keys=True).encode()
    nested_digest, _ = blob(nested_raw)
    files[f"blobs/sha256/{nested_digest.split(':')[1]}"] = nested_raw
    nested_descriptor = {"digest": nested_digest, "size": len(nested_raw)}
    if corrupt_nested_size:
        nested_descriptor["size"] += 1
    root = {"schemaVersion": 2, "manifests": [nested_descriptor] if nested else leaf_descriptors}
    files["index.json"] = json.dumps(root, sort_keys=True).encode()

    with tarfile.open(path, "w:") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))


class NestedOciIndexTest(unittest.TestCase):
    def test_existing_flat_index_selects_both_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flat.oci.tar"
            make_oci(path, nested=False)
            for platform in ("linux/amd64", "linux/arm64"):
                identity = release_candidate.verify_linux_image(path, platform, "e" * 40)
                self.assertEqual(identity["platform"], platform)

    def test_nested_index_selects_both_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.oci.tar"
            make_oci(path)
            amd64 = release_candidate.verify_linux_image(path, "linux/amd64", "e" * 40)
            arm64 = release_candidate.verify_linux_image(path, "linux/arm64", "e" * 40)
            self.assertEqual(amd64["platform"], "linux/amd64")
            self.assertEqual(arm64["platform"], "linux/arm64")
            self.assertNotEqual(amd64["manifest_digest"], arm64["manifest_digest"])

    def test_nested_index_descriptor_size_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.oci.tar"
            make_oci(path, corrupt_nested_size=True)
            with self.assertRaisesRegex(ValueError, "manifest size"):
                release_candidate.verify_linux_image(path, "linux/amd64", "e" * 40)


if __name__ == "__main__":
    unittest.main()
