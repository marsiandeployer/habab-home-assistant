import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest

import install
import update


class LinuxInstallTests(unittest.TestCase):
    def test_host_gate_requires_supported_ubuntu_python_and_arm64(self):
        good = {"ID": "ubuntu", "VERSION_ID": "26.04"}
        install.check_host(good, "aarch64", (3, 14, 2))
        with self.assertRaisesRegex(ValueError, "Ubuntu 26.04"):
            install.check_host({"ID": "raspbian", "VERSION_ID": "13"}, "aarch64", (3, 14, 2))
        with self.assertRaisesRegex(ValueError, "Python 3.14.2"):
            install.check_host(good, "aarch64", (3, 14, 1))

    def _archives(self):
        node_io = io.BytesIO()
        with tarfile.open(fileobj=node_io, mode="w:xz") as tar:
            data = b"node executable fixture"
            info = tarfile.TarInfo("node-v24/bin/node")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        node = node_io.getvalue()
        oc_io = io.BytesIO()
        with tarfile.open(fileobj=oc_io, mode="w:gz") as tar:
            data = b"opencode fixture"
            info = tarfile.TarInfo("package/bin/opencode")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
        return node, oc_io.getvalue()

    def _payload(self, root, version, source):
        payload = root / "payload"
        app = payload / "app"
        (payload / "linux").mkdir(parents=True)
        linux_dir = Path(__file__).parent
        for tool in ("install.py", "manage.py", "update.py", "runtime-pins.json"):
            shutil.copy2(linux_dir / tool, payload / "linux" / tool)
        (app / "frontend").mkdir(parents=True)
        (app / "config").mkdir()
        (app / "telegram-agent/vendor/noxon-telegram").mkdir(parents=True)
        (app / "config/configuration.yaml").write_text("default_config:\n")
        (app / "install-xiaomi-home.py").write_text("#!/usr/bin/env python3\n")
        (app / "xiaomi-home-release.json").write_text("{}\n")
        (app / "habab-ha.py").write_text("#!/usr/bin/env python3\n")
        for name in ("habab_home", "habab_telegram", "habab_yandex_speech", "idigital_sms"):
            (app / "src" / name).mkdir(parents=True)
        wheel = b"wheel-fixture"
        (app / "frontend/front.whl").write_bytes(wheel)
        (app / "frontend-release.json").write_text(json.dumps({"filename": "front.whl", "bytes": len(wheel), "sha256": hashlib.sha256(wheel).hexdigest()}))
        (payload / "release.json").write_text(json.dumps({"architecture": "arm64", "product_version": version, "source_sha": source, "home_assistant": "2026.10.2", "frontend": "2026.10.2"}))
        pins = {"node": {"version": "24.22.1", "url": "node", "sha256": ""}, "opencode": {"version": "1.14.21", "url": "oc", "integrity": ""}}
        node, oc = self._archives()
        pins["node"]["sha256"] = hashlib.sha256(node).hexdigest()
        import base64
        pins["opencode"]["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(oc).digest()).decode()
        (payload / "linux/runtime-pins.json").write_text(json.dumps(pins))
        return payload, {"node": node, "oc": oc}

    def _install(self, payload, archives, prefix):
        site = None
        def command(args, **kwargs):
            nonlocal site
            if args[1:3] == ["-c", "import sys; print('%s.%s.%s' % sys.version_info[:3])"]:
                return type("Result", (), {"stdout": "3.14.4\n"})()
            if args[1:3] == ["-m", "venv"]:
                venv = Path(args[-1])
                (venv / "bin").mkdir(parents=True)
                shutil.copy2(shutil.which("python3"), venv / "bin/python")
                site = venv.parent / "fake-site-packages"
                (site / "homeassistant/components").mkdir(parents=True, exist_ok=True)
            elif args[1:3] == ["-c", "import site; print(site.getsitepackages()[0])"]:
                return type("Result", (), {"stdout": str(site) + "\n"})()
            return type("Result", (), {"stdout": ""})()
        return install.install_release(payload, prefix, command, lambda url, maximum: archives[url], False)

    def test_install_update_rollback_preserves_config_and_switches_release_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            prefix = root / "root"
            first, archives = self._payload(root / "one", "1.0", "a" * 40)
            self._install(first, archives, prefix)
            config = prefix / "var/lib/habab-home/config"
            marker = config / "configuration.yaml"
            marker.write_text("my_existing_config: true\n")
            second, archives2 = self._payload(root / "two", "1.1", "b" * 40)
            self._install(second, archives2, prefix)
            opt = prefix / "opt/habab-home"
            self.assertEqual((opt / "current").resolve().name, "1.1-" + "b" * 12)
            self.assertEqual((opt / "previous").resolve().name, "1.0-" + "a" * 12)
            self.assertEqual(marker.read_text(), "my_existing_config: true\n")
            self.assertIn("HABAB_SOURCE_REVISION=" + "b" * 40, (opt / "current/runtime.env").read_text())
            self.assertTrue((prefix / "usr/local/bin/habab-ha").is_file())
            self.assertTrue((prefix / "usr/local/bin/habab-install-xiaomi-home").is_file())
            self.assertTrue((prefix / "usr/local/bin/xiaomi-home-release.json").is_file())
            install.rollback(prefix, perform_system_changes=False)
            self.assertEqual((opt / "current").resolve().name, "1.0-" + "a" * 12)
            self.assertIn("HABAB_SOURCE_REVISION=" + "a" * 40, (opt / "current/runtime.env").read_text())
            self.assertEqual(marker.read_text(), "my_existing_config: true\n")
            third, archives3 = self._payload(root / "three", "1.2", "c" * 40)
            self._install(third, archives3, prefix)
            self.assertEqual({item.name for item in (opt / "releases").iterdir()},
                             {"1.2-" + "c" * 12, "1.0-" + "a" * 12})
            self.assertEqual(marker.read_text(), "my_existing_config: true\n")

    def test_update_archive_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "bad.tar.gz"
            with tarfile.open(archive_path, "w:gz") as tar:
                data = b"bad"
                info = tarfile.TarInfo("payload/../../escape")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            with self.assertRaisesRegex(ValueError, "Unsafe path"):
                update.extract(archive_path, root / "out")

    def test_config_seed_does_not_replace_user_files(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config"
            config.mkdir()
            (config / "configuration.yaml").write_text("user: yes\n")
            (config / "automations.yaml").write_text("- id: mine\n")
            template = Path(temp) / "template.yaml"
            template.write_text("default_config:\n")
            install.ensure_config(config, template)
            self.assertEqual((config / "configuration.yaml").read_text(), "user: yes\n")
            self.assertEqual((config / "automations.yaml").read_text(), "- id: mine\n")


if __name__ == "__main__":
    unittest.main()
