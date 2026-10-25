"""Validate the Android network bridge before exposing URLs to assistant context."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from homeassistant.components.habab_home.android_network import (
    SOURCE,
    web_access_context,
)
from homeassistant.components.habab_home.runtime_context import ANDROID_PROOT_RUNTIME_KIND


def snapshot(*, updated_at=1000, web_access=None):
    return {
        "schema": 1,
        "updatedAt": updated_at,
        "interfaces": [],
        "web_access": web_access or {
            "phone_url": "http://127.0.0.1:8123",
            "lan_url": "http://192.168.1.42:8123",
            "lan_urls": ["http://192.168.1.42:8123", "http://10.0.0.8:8123"],
            "external_reachability": "not_verified",
            "source": SOURCE,
        },
    }


class AndroidNetworkContextTests(unittest.TestCase):
    def read(self, payload, *, now=1000, runtime_kind=ANDROID_PROOT_RUNTIME_KIND):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "android-network.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return web_access_context(path=path, now=now, runtime_kind=runtime_kind)

    def test_returns_only_validated_phone_and_private_lan_urls(self):
        result = self.read(snapshot())
        self.assertEqual(result, {
            "status": "available",
            "phone_url": "http://127.0.0.1:8123",
            "phone_url_scope": "адрес Home Assistant на этом Android-телефоне",
            "lan_url": "http://192.168.1.42:8123",
            "lan_urls": ["http://192.168.1.42:8123", "http://10.0.0.8:8123"],
            "lan_url_scope": "кандидат для клиента в той же локальной сети; доступность не проверена",
            "external_reachability": "not_verified",
            "source": "android_connectivity_manager",
        })

    def test_null_selected_url_keeps_candidates_without_claiming_one_pc_url(self):
        data = snapshot(web_access={
            "phone_url": "http://127.0.0.1:8123", "lan_url": None,
            "lan_urls": ["http://192.168.1.42:8123", "http://10.0.0.8:8123"],
            "external_reachability": "not_verified", "source": SOURCE,
        })
        result = self.read(data)
        self.assertIsNone(result["lan_url"])
        self.assertEqual(len(result["lan_urls"]), 2)

    def test_stale_missing_or_wrong_runtime_snapshot_is_explicitly_unknown(self):
        self.assertEqual(self.read(snapshot(updated_at=800), now=1000), {"status": "unknown"})
        self.assertEqual(self.read({"schema": 1, "updatedAt": 1000, "interfaces": []}),
                         {"status": "unknown"})
        self.assertEqual(self.read(snapshot(), runtime_kind="docker"), {"status": "unknown"})
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            self.assertEqual(web_access_context(path=missing, now=1000,
                                                runtime_kind=ANDROID_PROOT_RUNTIME_KIND),
                             {"status": "unknown"})

    def test_rejects_malformed_or_untrusted_url_contract_as_a_whole(self):
        for bad_url in (
            "https://192.168.1.42:8123",
            "http://homeassistant.local:8123",
            "http://192.168.1.42:80",
            "http://user@192.168.1.42:8123",
            "http://192.168.1.42:8123/private",
            "http://192.168.1.42:8123/?token=x",
            "http://127.0.0.1:8123",
            "http://169.254.1.2:8123",
            "http://8.8.8.8:8123",
        ):
            with self.subTest(url=bad_url):
                data = snapshot(web_access={
                    "phone_url": "http://127.0.0.1:8123", "lan_url": bad_url,
                    "lan_urls": [bad_url], "external_reachability": "not_verified",
                    "source": SOURCE,
                })
                self.assertEqual(self.read(data), {"status": "unknown"})

    def test_rejects_mismatched_source_or_unverified_reachability_claim_changes(self):
        for field, value in (("source", "other"), ("external_reachability", "reachable"),
                             ("phone_url", "http://10.0.0.1:8123")):
            with self.subTest(field=field):
                web = snapshot()["web_access"]
                web[field] = value
                self.assertEqual(self.read(snapshot(web_access=web)), {"status": "unknown"})

    def test_oversized_snapshot_read_is_capped_at_limit_plus_one_byte(self):
        read_sizes = []

        class BoundedRead(io.BytesIO):
            def read(self, size=-1):
                read_sizes.append(size)
                return super().read(size)

        with patch("builtins.open", return_value=BoundedRead(b" " * 65537)):
            result = web_access_context(
                path="unused.json", now=1000,
                runtime_kind=ANDROID_PROOT_RUNTIME_KIND,
            )
        self.assertEqual(read_sizes, [65537])
        self.assertEqual(result, {"status": "unknown"})


if __name__ == "__main__":
    unittest.main()
