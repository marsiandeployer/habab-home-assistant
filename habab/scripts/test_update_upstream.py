import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("update-upstream.sh")


class UpdateUpstreamTests(unittest.TestCase):
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
