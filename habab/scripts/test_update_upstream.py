import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("update-upstream.sh")


class UpdateUpstreamTests(unittest.TestCase):
    def test_fetches_and_merges_exact_local_upstream_tip_on_separate_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = root / "seed"
            seed.mkdir()
            self.git(seed, "init", "--initial-branch=main")
            self.git(seed, "config", "user.name", "QA")
            self.git(seed, "config", "user.email", "qa@example.invalid")
            (seed / "baseline.txt").write_text("shared base\n", encoding="utf-8")
            self.git(seed, "add", "baseline.txt")
            self.git(seed, "commit", "-m", "shared base")

            checkout = root / "checkout"
            self.git(root, "clone", str(seed), str(checkout))
            self.git(checkout, "config", "user.name", "QA")
            self.git(checkout, "config", "user.email", "qa@example.invalid")
            script_path = checkout / "habab/scripts/update-upstream.sh"
            script_path.parent.mkdir(parents=True)
            script_path.write_bytes(SCRIPT.read_bytes())
            self.git(checkout, "add", "habab/scripts/update-upstream.sh")
            self.git(checkout, "commit", "-m", "add updater under test")
            (checkout / "downstream.txt").write_text("fork change\n", encoding="utf-8")
            self.git(checkout, "add", "downstream.txt")
            self.git(checkout, "commit", "-m", "downstream change")
            downstream_sha = self.git(checkout, "rev-parse", "HEAD")

            upstream_work = root / "upstream-work"
            self.git(root, "clone", str(seed), str(upstream_work))
            self.git(upstream_work, "config", "user.name", "QA")
            self.git(upstream_work, "config", "user.email", "qa@example.invalid")
            self.git(upstream_work, "switch", "-c", "dev")
            (upstream_work / "upstream.txt").write_text("upstream change\n", encoding="utf-8")
            self.git(upstream_work, "add", "upstream.txt")
            self.git(upstream_work, "commit", "-m", "upstream change")
            upstream_sha = self.git(upstream_work, "rev-parse", "HEAD")

            bare_upstream = root / "upstream.git"
            self.git(root, "init", "--bare", "--initial-branch=dev", str(bare_upstream))
            self.git(upstream_work, "remote", "add", "upstream", str(bare_upstream))
            self.git(upstream_work, "push", "upstream", "dev")
            self.git(checkout, "remote", "add", "upstream",
                     "https://github.com/home-assistant/frontend.git")

            git_bin = shutil.which("git")
            self.assertIsNotNone(git_bin)
            wrapper_dir = root / "bin"
            wrapper_dir.mkdir()
            git_wrapper = wrapper_dir / "git"
            git_wrapper.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = fetch ]; then\n"
                f"  shift; exec {git_bin} -c 'url.file://{bare_upstream}.insteadOf="
                "https://github.com/home-assistant/frontend.git' fetch \"$@\"\n"
                "fi\n"
                f"exec {git_bin} \"$@\"\n",
                encoding="utf-8",
            )
            git_wrapper.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{wrapper_dir}{os.pathsep}{environment['PATH']}"
            environment["GIT_CONFIG_GLOBAL"] = os.devnull
            result = subprocess.run(
                ["sh", str(script_path)], cwd=checkout, env=environment,
                capture_output=True, text=True, timeout=10, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            branch = self.git(checkout, "branch", "--show-current")
            self.assertRegex(branch, re.compile(r"^update/home-assistant-\d{8}-\d{6}$"))
            self.assertEqual(self.git(checkout, "rev-parse", "HEAD^1"), downstream_sha)
            self.assertEqual(self.git(checkout, "rev-parse", "HEAD^2"), upstream_sha)
            self.assertEqual((checkout / "downstream.txt").read_text(encoding="utf-8"), "fork change\n")
            self.assertEqual((checkout / "upstream.txt").read_text(encoding="utf-8"), "upstream change\n")
            self.assertEqual(self.git(checkout, "status", "--porcelain"), "")

    def test_refuses_to_create_update_branch_from_non_main_without_fetching(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            script_path = repository / "habab/scripts/update-upstream.sh"
            script_path.parent.mkdir(parents=True)
            script_path.write_bytes(SCRIPT.read_bytes())

            self.git(repository, "init", "--initial-branch=release/test")
            self.git(repository, "config", "user.name", "QA")
            self.git(repository, "config", "user.email", "qa@example.invalid")
            (repository / "tracked.txt").write_text("baseline\n", encoding="utf-8")
            self.git(repository, "add", "tracked.txt", "habab/scripts/update-upstream.sh")
            self.git(repository, "commit", "-m", "baseline")
            self.git(repository, "remote", "add", "upstream",
                     "https://github.com/home-assistant/frontend.git")

            environment = os.environ.copy()
            environment["GIT_CONFIG_GLOBAL"] = os.devnull
            environment["GIT_TRACE"] = "1"
            result = subprocess.run(
                ["sh", str(script_path)], cwd=repository, env=environment,
                capture_output=True, text=True, timeout=5, check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Switch to main", result.stderr)
            self.assertEqual(self.git(repository, "branch", "--show-current"), "release/test")
            self.assertEqual(self.git(repository, "status", "--porcelain"), "")
            self.assertNotIn("fetch", result.stderr.lower())

    @staticmethod
    def git(repository, *arguments):
        result = subprocess.run(
            ["git", *arguments], cwd=repository,
            capture_output=True, text=True, timeout=5, check=True,
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
