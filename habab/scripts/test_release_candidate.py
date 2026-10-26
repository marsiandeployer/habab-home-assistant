import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile


SCRIPT = Path(__file__).with_name("release-candidate.py")
SPEC = importlib.util.spec_from_file_location("release_candidate", SCRIPT)
release_candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_candidate)


class ReleaseCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        temporary = Path(self.temporary.name)
        self.root = temporary / "artifacts"
        self.root.mkdir()
        self.source_tree = temporary / "source"
        distribution = self.source_tree / "habab/distribution"
        distribution.mkdir(parents=True)
        (distribution / "release.json").write_text(
            json.dumps({"product": "Habab Home", "version": "0.1"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (distribution / "start.bat").write_bytes(b"@echo off\nstart\n")
        self.revision = "a" * 40
        self.workflow_identity = {
            "workflow_revision": "c" * 40,
            "workflow_run_id": "12345",
            "workflow_run_attempt": "1",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_rejects_source_revisions_that_are_not_full_git_sha_lengths(self):
        for revision in ("a" * 39, "a" * 41, "a" * 63, "a" * 65):
            with self.subTest(length=len(revision)):
                with self.assertRaisesRegex(ValueError, "source revision must be a full Git SHA"):
                    release_candidate.assemble(
                        self.root, revision, "blocked", self.source_tree, "windows",
                        workflow_identity=self.workflow_identity,
                    )

    def add_status(self, target, state="built", **extra):
        directory = self.root / target
        directory.mkdir(parents=True, exist_ok=True)
        status = {"state": state, "source_revision": self.revision, "source_dirty": False,
                  **self.workflow_identity, **extra}
        if target in {"linux-amd64", "linux-arm64"}:
            platform = "linux/amd64" if target == "linux-amd64" else "linux/arm64"
            status.update({
                "platform": platform,
                "oci_image": {
                    "platform": platform,
                    "manifest_digest": "sha256:" + "a" * 64,
                    "config_digest": "sha256:" + "b" * 64,
                    "verified_labels": {
                        "org.opencontainers.image.revision": self.revision,
                        "io.habab.source.dirty": "false",
                    },
                },
            })
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")
        if state == "built":
            (directory / "artifact.bin").write_bytes(b"artifact:" + target.encode())

    def fill_targets(self, android_state="blocked"):
        self.add_windows_package()
        self.add_status("linux-amd64")
        self.add_status("linux-arm64")
        self.add_status("android", android_state,
                        **({"reason": "signed runtime input is not available"} if android_state == "blocked" else {}))

    def add_windows_package(self, extra_members=()):
        directory = self.root / "windows"
        directory.mkdir(parents=True, exist_ok=True)
        source = self.source_tree / "habab/distribution"
        release = json.loads((source / "release.json").read_text(encoding="utf-8"))
        release.update({"source_revision": self.revision, "source_dirty": "false"})
        release_bytes = (json.dumps(release, indent=2) + "\n").encode()
        filename = "habab-home-windows-test.zip"
        members = {"release.json": release_bytes, "start.bat": b"@echo off\r\nstart\r\n"}
        for name in extra_members:
            source_file = release_candidate.resolve_windows_package_source(source, name)
            members[name] = source_file.read_bytes()
        with zipfile.ZipFile(directory / filename, "w", compression=zipfile.ZIP_DEFLATED) as package:
            for name, content in members.items():
                package.writestr(name, content)
        data = (directory / filename).read_bytes()
        metadata = {"filename": filename, "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data), "files": sorted(members)}
        (directory / "windows-package.json").write_text(json.dumps(metadata), encoding="utf-8")
        for name in ("start-home-assistant.bat", "start-xiaomi-home.bat", "support_tunnel.bat"):
            (directory / name).write_text(f"{filename} {metadata['sha256']}", encoding="ascii")
        status = {"state": "built", "source_revision": self.revision,
                  "source_dirty": False, **self.workflow_identity,
                  "kind": "installer-package", "filename": filename}
        (directory / "status.json").write_text(json.dumps(status), encoding="utf-8")

    def test_windows_candidate_verifies_only_the_approved_external_tunnel_files(self):
        product_tunnel = self.source_tree / "habab/product-tunnel"
        product_tunnel.mkdir(parents=True)
        external_members = {
            "product-tunnel/control.ps1": b"control client\n",
            "product-tunnel/connect.ps1": b"connect client\n",
            "product-tunnel/home.example.json": b"{}\n",
        }
        for name, content in external_members.items():
            (product_tunnel / Path(name).name).write_bytes(content)
        (product_tunnel / "identity").write_bytes(b"private key material")
        (product_tunnel / "unreviewed.txt").write_text("outside the allowlist")
        distribution = self.source_tree / "habab/distribution"
        (distribution / "product-tunnel").mkdir()
        (distribution / "product-tunnel/unreviewed.txt").write_text("distribution source")
        self.add_windows_package(external_members)

        manifest = release_candidate.assemble(
            self.root, self.revision, "blocked", self.source_tree, "windows",
            workflow_identity=self.workflow_identity,
        )

        self.assertIn("windows", manifest["targets"])
        with zipfile.ZipFile(self.root / "windows/habab-home-windows-test.zip") as package:
            packaged_paths = set(package.namelist())
        self.assertTrue(set(external_members) <= packaged_paths)
        self.assertNotIn("product-tunnel/identity", packaged_paths)

        with self.assertRaisesRegex(ValueError, "Unsafe Windows package member path"):
            release_candidate.resolve_windows_package_source(distribution, "../product-tunnel/identity")
        self.assertEqual(
            release_candidate.resolve_windows_package_source(distribution, "product-tunnel/unreviewed.txt"),
            distribution / "product-tunnel/unreviewed.txt",
        )

    def test_assembles_hashes_and_keeps_android_blocked_explicit(self):
        self.fill_targets()

        manifest = release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree,
                                              workflow_identity=self.workflow_identity)

        self.assertFalse(manifest["source_dirty"])
        self.assertEqual(manifest["targets"]["android"]["state"], "blocked")
        self.assertEqual(len(manifest["artifacts"]), 7)
        hashes = {item["path"]: item["sha256"] for item in manifest["artifacts"]}
        self.assertEqual(hashes["linux-amd64/artifact.bin"],
                         release_candidate._digest(self.root / "linux-amd64/artifact.bin")[0])
        records = {item["path"]: item for item in manifest["artifacts"]}
        self.assertEqual(records["linux-amd64/artifact.bin"]["artifact"], "candidate-linux-amd64")
        self.assertEqual(manifest["targets"]["windows"]["artifact"], "candidate-windows")

    def test_all_scope_includes_android_payload_when_android_is_built(self):
        self.fill_targets(android_state="built")

        manifest = release_candidate.assemble(
            self.root, self.revision, "built", self.source_tree,
            workflow_identity=self.workflow_identity)

        self.assertEqual(manifest["targets"]["android"]["state"], "built")
        self.assertIn(
            {"artifact": "candidate-android", "path": "android/artifact.bin",
             "sha256": release_candidate._digest(self.root / "android/artifact.bin")[0],
             "bytes": len(b"artifact:android")},
            manifest["artifacts"],
        )

    def test_rejects_target_with_a_different_source_revision(self):
        self.fill_targets()
        path = self.root / "linux-arm64/status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        status["source_revision"] = "b" * 40
        path.write_text(json.dumps(status), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "another source revision"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree,
                                      workflow_identity=self.workflow_identity)

    def test_rejects_missing_workflow_identity(self):
        self.fill_targets()
        path = self.root / "linux-amd64/status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        del status["workflow_run_attempt"]
        path.write_text(json.dumps(status), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "workflow identity does not match"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree,
                                      workflow_identity=self.workflow_identity)

    def test_rejects_mismatched_workflow_identity(self):
        self.fill_targets()
        path = self.root / "linux-arm64/status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        status["workflow_run_id"] = "67890"
        path.write_text(json.dumps(status), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "workflow identity does not match"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree,
                                      workflow_identity=self.workflow_identity)

    def test_windows_scope_omits_unselected_platforms(self):
        self.add_windows_package()

        manifest = release_candidate.assemble(
            self.root, self.revision, "blocked", self.source_tree, "windows",
            workflow_identity=self.workflow_identity)

        self.assertEqual(set(manifest["targets"]), {"windows"})
        self.assertTrue(all(item["artifact"] == "candidate-windows" for item in manifest["artifacts"]))

    def test_rejects_invalid_target_scope(self):
        with self.assertRaisesRegex(ValueError, "target scope must be"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree, "desktop",
                                      workflow_identity=self.workflow_identity)

    def test_rejects_missing_selected_linux_architecture(self):
        self.fill_targets()
        (self.root / "linux-arm64/status.json").unlink()

        with self.assertRaisesRegex(ValueError, "Missing linux-arm64 status readback"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree, "linux",
                                      workflow_identity=self.workflow_identity)

    def test_rejects_dirty_source_attestation(self):
        self.fill_targets()
        path = self.root / "windows/status.json"
        status = json.loads(path.read_text(encoding="utf-8"))
        status["source_dirty"] = True
        path.write_text(json.dumps(status), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "clean source tree"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree,
                                      workflow_identity=self.workflow_identity)

    def test_rejects_windows_package_that_breaks_source_copy_contract(self):
        self.fill_targets()
        path = self.root / "windows/habab-home-windows-test.zip"
        release = json.loads((self.source_tree / "habab/distribution/release.json").read_text(encoding="utf-8"))
        release.update({"source_revision": self.revision, "source_dirty": "false"})
        release_bytes = (json.dumps(release, indent=2) + "\n").encode()
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as package:
            package.writestr("release.json", release_bytes)
            package.writestr("start.bat", b"@echo off\r\nchanged\r\n")
        metadata_path = self.root / "windows/windows-package.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        data = path.read_bytes()
        metadata.update({"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "differs from selected source"):
            release_candidate.assemble(self.root, self.revision, "blocked", self.source_tree,
                                      workflow_identity=self.workflow_identity)

    def test_android_workflow_accepts_and_rejects_locked_node_inventory(self):
        repository = Path(__file__).resolve().parents[2]
        workflow = (repository / ".github/workflows/habab-release-candidate.yaml").read_text()
        blocks = re.findall(r"(?ms)^[ \t]+python3 - <<'PY'\n(.*?)^[ \t]+PY[ \t]*$", workflow)
        validator = next((textwrap.dedent(block) for block in blocks if "node_lock = json.loads(Path(" in block), None)
        self.assertIsNotNone(validator, "Android runtime validation block is missing from the workflow")

        revision = "c" * 40
        with tempfile.TemporaryDirectory(prefix="ci14-node-inventory-") as temporary:
            root = Path(temporary)
            distribution = root / "habab/distribution"
            for name in ("habab_home", "habab_telegram", "habab_yandex_speech", "idigital_sms"):
                (distribution / "src" / name).mkdir(parents=True)
            (distribution / "telegram-agent").mkdir()
            (distribution / "config").mkdir()
            fixed = {
                "habab-ha.py": b"ha\n",
                "install-xiaomi-home.py": b"install\n",
                "xiaomi-home-release.json": b"{}\n",
            }
            for name, content in fixed.items():
                (distribution / name).write_bytes(content)
            config = "default_config:\n"
            (distribution / "config/configuration.yaml").write_text(config)
            launcher = root / "habab/android/runtime/launch.py"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("print('launcher')\n")
            node_lock_path = root / "habab/android/tools/network_tools/node-apks.lock.json"
            node_lock_path.parent.mkdir(parents=True)
            (root / "habab/android/tools/package_runtime.py").write_text(
                (repository / "habab/android/tools/package_runtime.py").read_text())
            node_lock = {"packages": {
                "nodejs-24.18.1-r0.apk": {"sha256": "a" * 64},
                "icu-data-en-78.1-r0.apk": {"sha256": "b" * 64},
            }}
            node_lock_path.write_text(json.dumps(node_lock))
            appended_config = config + "\nscript: !include scripts.yaml\nscene: !include scenes.yaml\nrecorder:\nhistory:\n"
            release = {
                "sourceRevision": revision,
                "sourceFiles": {name: hashlib.sha256(content).hexdigest() for name, content in fixed.items()},
                "nodePackages": {name: item["sha256"] for name, item in node_lock["packages"].items()},
                "androidLauncherSha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
                "configurationSha256": hashlib.sha256(appended_config.encode()).hexdigest(),
                "url": "https://habab-demo.wpmix.net/downloads/android/runtime.tgz",
            }
            runtime = root / "runtime-input/release.json"
            runtime.parent.mkdir()
            runtime.write_text(json.dumps(release))
            environment = {**os.environ, "SOURCE_REVISION": revision}

            accepted = subprocess.run([sys.executable, "-c", validator], cwd=root, env=environment,
                                      capture_output=True, text=True, check=False)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

            release["nodePackages"]["nodejs-24.18.1-r0.apk"] = "f" * 64
            runtime.write_text(json.dumps(release))
            rejected = subprocess.run([sys.executable, "-c", validator], cwd=root, env=environment,
                                      capture_output=True, text=True, check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("Node APK inventory does not match", rejected.stderr)

    def test_all_scope_attempts_android_and_records_missing_inputs_as_blocked(self):
        repository = Path(__file__).resolve().parents[2]
        workflow = (repository / ".github/workflows/habab-release-candidate.yaml").read_text()

        self.assertIn("if: ${{ inputs.target_scope == 'all' || inputs.target_scope == 'android' }}", workflow)
        self.assertIn("state: ${{ steps.inputs.outputs.state }}", workflow)
        self.assertIn("'state': 'blocked'", workflow)
        self.assertIn("explicit Android version code/name, runtime signing key (or matching signed runtime) and all APK release signing inputs are required", workflow)
        self.assertIn("needs.android.outputs.state", workflow)

    def test_android_prerequisite_gate_blocks_before_build_and_accepts_runtime_key(self):
        workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/habab-release-candidate.yaml").read_text()
        start = workflow.index("        run: |\n", workflow.index("        name: Gate Android runtime and signing inputs")) + len("        run: |\n")
        stop = workflow.index("\n      - uses:", start)
        script = textwrap.dedent(workflow[start:stop])
        apk = {
            "ANDROID_KEYSTORE_BASE64": "test-present",
            "ANDROID_KEYSTORE_PASSWORD": "test-present",
            "ANDROID_KEY_ALIAS": "test-present",
            "ANDROID_KEY_PASSWORD": "test-present",
        }
        versions = {"ANDROID_VERSION_CODE": "34", "ANDROID_VERSION_NAME": "0.2.4-preview"}
        complete = {**apk, **versions, "RUNTIME_SIGNING_KEY_BASE64": "test-present"}
        cases = (
            ("all", {}, 0, "blocked"),
            ("android", {}, 1, None),
            ("all", complete, 0, "built"),
            ("all", {**complete, "ANDROID_VERSION_CODE": ""}, 0, "blocked"),
            ("android", {**complete, "ANDROID_VERSION_NAME": ""}, 1, None),
            ("android", {**complete, "ANDROID_VERSION_CODE": "2100000001"}, 1, None),
            ("android", {**complete, "ANDROID_VERSION_CODE": "01"}, 1, None),
            ("android", {**complete, "ANDROID_VERSION_NAME": "invalid version"}, 1, None),
            ("android", {**apk, **versions, "RUNTIME_MANIFEST_URL": "https://example.invalid/old.json"}, 1, None),
        )
        for scope, configured, returncode, state in cases:
            with self.subTest(scope=scope, state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / "outputs"
                environment = {**os.environ, **dict.fromkeys((*apk, *versions, "RUNTIME_SIGNING_KEY_BASE64", "RUNTIME_MANIFEST_URL"), ""),
                               **configured, "GITHUB_OUTPUT": str(output), "SOURCE_REVISION": self.revision,
                               "GITHUB_WORKFLOW_SHA": self.workflow_identity["workflow_revision"],
                               "GITHUB_RUN_ID": self.workflow_identity["workflow_run_id"],
                               "GITHUB_RUN_ATTEMPT": self.workflow_identity["workflow_run_attempt"]}
                result = subprocess.run(["bash", "-c", script.replace("${{ inputs.target_scope }}", scope)],
                                        cwd=root, env=environment, capture_output=True, text=True)
                self.assertEqual(result.returncode, returncode, result.stderr)
                if state is not None:
                    self.assertIn("state=" + state, output.read_text())
                if state == "blocked":
                    status = json.loads((root / "candidate/android/status.json").read_text())
                    self.assertEqual(status["source_revision"], self.revision)
                    self.assertEqual(status["state"], "blocked")

    def test_aggregate_android_state_branch_handles_skipped_blocked_and_built(self):
        repository = Path(__file__).resolve().parents[2]
        workflow = (repository / ".github/workflows/habab-release-candidate.yaml").read_text()
        marker = "      - name: Record checksums and explicit platform status\n"
        aggregate_start = workflow.index(marker)
        run_start = workflow.index("        run: |\n", aggregate_start) + len("        run: |\n")
        next_step = workflow.index("\n      - uses:", run_start)
        aggregate = "\n".join(
            line[10:] if line.startswith("          ") else line
            for line in workflow[run_start:next_step].splitlines()
        )
        start = aggregate.index('if [[ -z "$ANDROID_RESULT" ]]; then')
        end_marker = '\npython3 habab/scripts/release-candidate.py '
        branch = aggregate[start:aggregate.index(end_marker, start)]

        for android_result, expected_state in (("", "blocked"), ("blocked", "blocked"), ("built", "built")):
            with self.subTest(android_result=android_result), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = root / "github-output"
                environment = {
                    **os.environ,
                    "ANDROID_RESULT": android_result,
                    "SOURCE_REVISION": self.revision,
                    "GITHUB_WORKFLOW_SHA": self.workflow_identity["workflow_revision"],
                    "GITHUB_RUN_ID": self.workflow_identity["workflow_run_id"],
                    "GITHUB_RUN_ATTEMPT": self.workflow_identity["workflow_run_attempt"],
                    "GITHUB_OUTPUT": str(output),
                }
                result = subprocess.run(
                    ["bash", "-c", "set -euo pipefail\nmkdir -p candidate/android\n" + branch + '\nprintf \'%s\\n\' "$android_state"'],
                    cwd=root, env=environment, capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected_state)
                if not android_result:
                    status = json.loads((root / "candidate/android/status.json").read_text())
                    self.assertEqual(status["state"], "blocked")
                    self.assertEqual(status["source_revision"], self.revision)


class ReleasePreflightWorkflowTests(unittest.TestCase):
    @staticmethod
    def workflow_text():
        repository = Path(__file__).resolve().parents[2]
        return (repository / ".github/workflows/habab-release-candidate.yaml").read_text()

    def test_preflight_blocks_every_target_and_final_manifest(self):
        workflow = self.workflow_text()
        self.assertIn("  release-preflight:\n", workflow)
        self.assertIn("  release-preflight:\n    name: Run fast source checks\n    needs: resolve-source", workflow)
        for job in ("windows-source", "linux-image", "android-inputs"):
            with self.subTest(job=job):
                self.assertRegex(
                    workflow,
                    rf"(?ms)^  {job}:\n    needs: \[resolve-source, release-preflight\]$",
                )
        assemble = workflow[workflow.index("  assemble-candidate:\n"):]
        self.assertIn("needs: [resolve-source, release-preflight, windows-source, linux-image, android]", assemble)
        self.assertIn("needs.release-preflight.result == 'success'", assemble)
        self.assertNotRegex(assemble, r"(?m)^\s*continue-on-error:")

    def test_fastchecks_use_a_pinned_offline_container_and_builtin_node_tests(self):
        workflow = self.workflow_text()
        preflight = workflow[workflow.index("  release-preflight:\n"):workflow.index("  windows-source:\n")]
        self.assertIn(
            "ghcr.io/home-assistant/home-assistant:2026.10.2@sha256:"
            "33afb0659d786213264c2211f8d91c5fe87461c56eba6a3a546eb05fc71e89f4",
            preflight,
        )
        for value in (
            "--network none", "--memory=1g", "--memory-swap=1g", "--cpus=1",
            "--pids-limit=64", "--entrypoint python3", ":ro",
            "tests/run_release_candidate_backend_tests.py",
        ):
            with self.subTest(value=value):
                self.assertIn(value, preflight)
        self.assertIn(
            "actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1",
            preflight,
        )
        js_step = preflight[preflight.index("      - name: Run focused native JavaScript tests"):]
        for test in (
            "test-automation-setup.mjs", "test-chat-lifecycle.mjs",
            "test-integration-setup.mjs", "test-provider-selection.mjs",
        ):
            self.assertIn(test, js_step)
        self.assertNotRegex(preflight, r"(?m)^\s*continue-on-error:")

    def test_preflight_requires_runner_and_approved_tracked_public_trust_anchor(self):
        repository = Path(__file__).resolve().parents[2]
        workflow = self.workflow_text()
        preflight = workflow[workflow.index("  release-preflight:\n"):workflow.index("  windows-source:\n")]
        runner = "habab/distribution/tests/run_release_candidate_backend_tests.py"
        self.assertIn("habab/android/app/src/main/assets/release", preflight)
        self.assertIn(f"[[ -f {runner} ]]", preflight)
        self.assertIn("grep -Fq 'class ReleasePreflightWorkflowTests'", preflight)
        self.assertIn("choose a revision with release fast checks", preflight)
        self.assertIn("git ls-files --error-unmatch \"$trust_anchor\"", preflight)
        self.assertIn(
            "ee1c6fbd9f95d1a3396cc68c103195e6e4fdd17349476c6599e7efad5ace9069",
            preflight,
        )
        self.assertIn("openssl pkey -pubin -inform DER", preflight)

        anchor_relative = "habab/android/app/src/main/assets/release/trusted-key.der"
        anchor = repository / anchor_relative
        self.assertTrue(anchor.is_file(), "Approved public runtime trust anchor is absent")
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", anchor_relative],
            cwd=repository, capture_output=True, text=True, check=False,
        )
        self.assertEqual(tracked.returncode, 0, "Public runtime trust anchor is not tracked")
        self.assertEqual(
            hashlib.sha256(anchor.read_bytes()).hexdigest(),
            "ee1c6fbd9f95d1a3396cc68c103195e6e4fdd17349476c6599e7efad5ace9069",
        )

    def test_android_unit_gate_runs_after_signed_inputs_and_before_assembly(self):
        workflow = self.workflow_text()
        runtime_check = workflow.index("      - name: Verify same-source signed runtime input")
        native_inputs = workflow.index("      - name: Prepare pinned APK native tools and voice model")
        unit_tests = workflow.index("      - name: Run Android unit tests before APK packaging")
        assembly = workflow.index("      - name: Lint and assemble Android APK")
        self.assertLess(runtime_check, native_inputs)
        self.assertLess(native_inputs, unit_tests)
        self.assertLess(unit_tests, assembly)
        before_unit_tests = workflow[:unit_tests]
        for invariant in (
            "openssl dgst -sha256 -verify", "release.get('sourceRevision')",
            "release.get('sourceFiles') != expected", "release.get('nodePackages')",
            "androidLauncherSha256", "--frontend-candidate frontend-input",
            "configurationSha256", "trusted-key.der", "default-url.txt",
        ):
            self.assertIn(invariant, before_unit_tests)
        unit_step = workflow[unit_tests:assembly]
        self.assertIn(":app:testDebugUnitTest", unit_step)
        self.assertNotIn(":app:assembleRelease", unit_step)
        sign_step = workflow.index("      - name: Sign APK with release keystore")
        self.assertGreater(sign_step, assembly)
        self.assertIn("verify_apk_provenance", workflow[sign_step:])
        self.assertIn("verify_apk_version", workflow[sign_step:])

    def test_android_release_frontend_version_matches_native_candidate_defaults(self):
        repository = Path(__file__).resolve().parents[2]
        release_workflow = self.workflow_text()
        frontend_job = release_workflow[
            release_workflow.index("  android-frontend:\n"):release_workflow.index("  android:\n")
        ]
        release_version = re.search(r"(?m)^      wheel_version: (\S+)$", frontend_job)
        self.assertIsNotNone(release_version)
        self.assertEqual(release_version.group(1), "20260930.4+habab.17")

        native_workflow = (repository / ".github/workflows/habab-native-frontend-candidate.yaml").read_text()
        input_definitions = native_workflow[:native_workflow.index("permissions:\n")]
        native_defaults = re.findall(r"(?m)^        default: (20260930\.4\+habab\.\d+)$", input_definitions)
        self.assertEqual(native_defaults, [release_version.group(1), release_version.group(1)])


if __name__ == "__main__":
    unittest.main()
