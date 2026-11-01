"""Cheap harness checks; these do not claim to start Core or test live models."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from run_core_api_e2e import CheckFailed, INVALID_KEY, VALID_KEY, ProviderFixture, require, stop_child


class CoreAPIE2EHarnessTests(unittest.TestCase):
    def test_http_failure_status_is_strict_and_only_retains_safe_integer_metadata(self):
        for status in (200, 404, 503):
            with self.subTest(status=status):
                with self.assertRaises(CheckFailed) as caught:
                    require(status == 401, "unauthenticated_status_allowed", http_status=status)
                self.assertEqual(caught.exception.http_status, status)
                self.assertEqual(str(caught.exception), "unauthenticated_status_allowed")
        require(True, "unauthenticated_status_allowed", http_status=401)
        with self.assertRaises(CheckFailed) as caught:
            require(False, "fixed_error", http_status="response body must not be retained")
        self.assertIsNone(caught.exception.http_status)

    def request(self, fixture, *, key=VALID_KEY, path="/openai/chat/completions"):
        payload = {"model": "fixture-model", "messages": [{"role": "user", "content": "probe"}],
                   "tools": [], "tool_choice": "required", "thinking": {"type": "disabled"}}
        request = urllib.request.Request(fixture.base + path, data=json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_fixture_exercises_http_tool_contract_without_retaining_credentials(self):
        with ProviderFixture() as fixture:
            status, body = self.request(fixture)
            self.assertEqual(status, 200)
            function = body["choices"][0]["message"]["tool_calls"][0]["function"]
            self.assertEqual(function["name"], "habab_connection_test")
            self.assertEqual(json.loads(function["arguments"]), {"ok": True})
            self.assertEqual(fixture.requests[0]["tool_choice"], "required")
            self.assertTrue(fixture.requests[0]["thinking_disabled"])
            self.assertNotIn(VALID_KEY, json.dumps(fixture.requests))

    def test_fixture_rejects_auth_and_unknown_transport(self):
        with ProviderFixture() as fixture:
            self.assertEqual(self.request(fixture, key=INVALID_KEY)[0], 401)
            self.assertEqual(self.request(fixture, path="/unexpected")[0], 404)
            self.assertNotIn(INVALID_KEY, json.dumps(fixture.requests))

    def test_fixture_failures_cross_real_http_boundary(self):
        with ProviderFixture() as fixture:
            fixture.mode = "quota"
            self.assertEqual(self.request(fixture)[0], 429)
            fixture.mode = "no_tools"
            status, body = self.request(fixture)
            self.assertEqual(status, 200)
            self.assertNotIn("tool_calls", body["choices"][0]["message"])
            fixture.mode = "invalid_response"
            self.assertEqual(self.request(fixture), (200, []))

    def test_runner_refuses_non_actions_execution_before_starting_core(self):
        environment = dict(os.environ)
        environment.pop("GITHUB_ACTIONS", None)
        result = subprocess.run([sys.executable, str(Path(__file__).with_name("run_core_api_e2e.py"))],
            env=environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("disposable GitHub Actions preflight container", result.stderr)

    @unittest.skipUnless(hasattr(os, "killpg"), "Runner cleanup uses Linux process groups")
    def test_child_cleanup_actually_stops_its_own_process(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                   start_new_session=True)
        try:
            started = time.monotonic()
            stop_child(process)
            self.assertIsNotNone(process.poll())
            self.assertLess(time.monotonic() - started, 5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
