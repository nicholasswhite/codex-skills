"""Deterministic tests for the portable handoff state collector."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
STATE_SCRIPT = SCRIPTS_DIR / "handoff_state.py"
sys.path.insert(0, str(SCRIPTS_DIR))

import handoff_state  # noqa: E402


def run_state_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the public CLI exactly as a caller of the skill would."""
    return subprocess.run(
        [sys.executable, str(STATE_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


class NonGitStateTests(unittest.TestCase):
    def test_non_git_capture_does_not_fabricate_refs_or_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            facts = handoff_state.capture(temp_dir)

            self.assertEqual("filesystem", facts["mode"])
            self.assertIsNone(facts["branch"])
            self.assertIsNone(facts["tip"])
            self.assertIsNone(facts["default_branch"])
            self.assertIsNone(facts["since_ref"])
            self.assertIsNone(facts["ahead"])
            self.assertEqual([], facts["commits_since"])
            self.assertEqual(0, facts["commit_count_since"])

            markdown = handoff_state.render_state_markdown(facts)
            self.assertIn("non-Git mode", markdown)
            self.assertIn("branch, tip, and commit history are unavailable", markdown)
            self.assertNotIn("`main`", markdown)
            self.assertNotIn("`master`", markdown)

    def test_non_git_cli_returns_json_without_fabricated_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_state_cli("capture", "--path", temp_dir, "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual("filesystem", facts["mode"])
        self.assertIsNone(facts["default_branch"])
        self.assertIsNone(facts["since_ref"])
        self.assertEqual([], facts["commits_since"])

    def test_non_git_commits_cli_reports_no_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_state_cli("commits", "--path", temp_dir, "--json")

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], json.loads(result.stdout))

    def test_missing_path_is_a_usage_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "not-created"
            result = run_state_cli("capture", "--path", str(missing), "--json")

        self.assertEqual(2, result.returncode)
        self.assertIn("path does not exist", result.stderr)
        self.assertEqual("", result.stdout)


@unittest.skipUnless(shutil.which("git"), "git is required for integration fixtures")
class GitStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.repo = Path(self._temp.name)
        self.git_env = os.environ.copy()
        self.git_env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTHOR_DATE": "2024-01-01T12:00:00+00:00",
                "GIT_COMMITTER_DATE": "2024-01-01T12:00:00+00:00",
            }
        )

        self.git("init")
        self.git("branch", "-M", "main")
        self.git("config", "user.name", "Session Handoff Tests")
        self.git("config", "user.email", "handoff-tests@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.autocrlf", "false")

        (self.repo / "README.md").write_text("baseline\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "--no-verify", "-m", "baseline")
        self.main_tip = self.git("rev-parse", "--short", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self.git_env,
            check=False,
        )
        if result.returncode != 0:
            self.fail(
                f"git {' '.join(args)} failed ({result.returncode}):\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}"
            )
        return result

    def make_feature_commit(self) -> str:
        self.git("switch", "-c", "feature/reminders")
        (self.repo / "feature.txt").write_text("portable handoff\n", encoding="utf-8")
        self.git("add", "feature.txt")
        self.git("commit", "--no-verify", "-m", "add portable handoff")
        return self.git("rev-parse", "--short", "HEAD").stdout.strip()

    def test_discovers_main_and_captures_clean_feature_comparison(self) -> None:
        feature_tip = self.make_feature_commit()

        facts = handoff_state.capture(str(self.repo), since="main")

        self.assertEqual("git", facts["mode"])
        self.assertEqual(self.repo.resolve(), Path(facts["repo_root"]))
        self.assertEqual("feature/reminders", facts["branch"])
        self.assertEqual(feature_tip, facts["tip"])
        self.assertEqual("main", facts["default_branch"])
        self.assertEqual("main", facts["comparison_ref"])
        self.assertEqual("main", facts["since_ref"])
        self.assertEqual(1, facts["ahead"])
        self.assertFalse(facts["dirty"])
        self.assertEqual(0, facts["changed"])
        self.assertEqual(1, facts["commit_count_since"])
        self.assertEqual("add portable handoff", facts["commits_since"][0]["subject"])

    def test_markdown_omits_live_tip_hash(self) -> None:
        feature_tip = self.make_feature_commit()
        facts = handoff_state.capture(str(self.repo), since="main")

        markdown = handoff_state.render_state_markdown(facts)

        self.assertNotIn(feature_tip, markdown)
        self.assertNotIn(self.main_tip, markdown)
        self.assertIn("branch `feature/reminders`", markdown)
        self.assertIn("1 commit(s) ahead of `main`", markdown)
        self.assertIn("Do not trust this last-known state blindly", markdown)

    def test_dirty_status_reports_untracked_file(self) -> None:
        self.make_feature_commit()
        (self.repo / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

        facts = handoff_state.capture(str(self.repo))

        self.assertTrue(facts["dirty"])
        self.assertEqual(1, facts["changed"])
        self.assertIn("scratch.txt", facts["changed_files"])

    def test_invalid_default_and_since_refs_fail_without_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "default branch/ref does not exist"):
            handoff_state.capture(str(self.repo), default_branch="does-not-exist")
        with self.assertRaisesRegex(ValueError, "session-start ref does not exist"):
            handoff_state.capture(str(self.repo), since="does-not-exist")

        for option in ("--default", "--since"):
            with self.subTest(option=option):
                result = run_state_cli(
                    "capture",
                    "--path",
                    str(self.repo),
                    option,
                    "does-not-exist",
                    "--json",
                )
                self.assertEqual(2, result.returncode)
                self.assertIn("does not exist", result.stderr)
                self.assertEqual("", result.stdout)

    def test_successful_git_cli_has_zero_exit_code_and_structured_output(self) -> None:
        self.make_feature_commit()
        result = run_state_cli(
            "capture", "--path", str(self.repo), "--since", "main", "--json"
        )

        self.assertEqual(0, result.returncode, result.stderr)
        facts = json.loads(result.stdout)
        self.assertEqual("git", facts["mode"])
        self.assertEqual("feature/reminders", facts["branch"])
        self.assertEqual(1, facts["commit_count_since"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
