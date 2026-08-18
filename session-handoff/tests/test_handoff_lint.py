"""Tests for strict handoff validation and pickup-prompt rendering."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
LINT_SCRIPT = SCRIPTS_DIR / "handoff_lint.py"
sys.path.insert(0, str(SCRIPTS_DIR))

import handoff_lint  # noqa: E402


VALID_HANDOFF = """# Read first

Read `AGENTS.md`, then this handoff.

# Current state

Verify the live state and rerun the project checks before acting.

# What the last session did

Added a portable, read-only state collector.

# Constraints

Do not commit unless explicitly requested and project policy permits it.
Do not push; pushing is not authorized.

# Next executable step

Run the focused test suite and inspect its output.
"""


def valid_prompt_spec() -> dict:
    return {
        "project": "Portable session handoff",
        "read_order": [
            {"path": "AGENTS.md", "note": "project policy"},
            "HANDOFF.md",
        ],
        "state_lines": ["The last focused test run passed."],
        "accomplishments": ["Added Git and non-Git state capture."],
        "constraints": [
            "Do not commit unless explicitly requested and project policy permits it.",
            "Do not push; pushing is not authorized.",
        ],
        "threads": [
            {"title": "Forward test", "desc": "Exercise the skill in a fixture."}
        ],
    }


def run_lint_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LINT_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


class StrictLintTests(unittest.TestCase):
    def test_complete_handoff_passes_strict_lint(self) -> None:
        report = handoff_lint.lint(
            VALID_HANDOFF, branch="feature/reminders", default_branch="main"
        )

        self.assertTrue(report.ok())
        self.assertTrue(report.ok(strict=True), report.to_dict())
        self.assertEqual([], report.errors)
        self.assertEqual([], report.warnings)

    def test_missing_required_section_is_an_error(self) -> None:
        invalid = VALID_HANDOFF.replace(
            "# What the last session did\n\nAdded a portable, read-only state collector.\n\n",
            "",
        )

        report = handoff_lint.lint(invalid)

        self.assertFalse(report.ok())
        self.assertIn("missing required section: 'last session'", report.errors)

    def test_headings_inside_fenced_examples_do_not_satisfy_structure(self) -> None:
        fenced = """```markdown
# Read first
# Current state
# What the last session did
# Constraints
# Next executable step
```
"""

        report = handoff_lint.lint(fenced)

        self.assertFalse(report.ok())
        self.assertEqual(5, len(report.errors))

    def test_negative_verify_instruction_does_not_satisfy_reverification(self) -> None:
        negative = VALID_HANDOFF.replace(
            "Verify the live state and rerun the project checks before acting.",
            "Do not verify anything.",
        )

        report = handoff_lint.lint(negative)

        self.assertFalse(report.ok(strict=True))
        self.assertTrue(any("no verify/re-derive" in item for item in report.warnings))

    def test_pinned_current_hash_is_warning_and_strict_failure(self) -> None:
        pinned = VALID_HANDOFF.replace(
            "Verify the live state", "Current HEAD is abcdef1. Verify the live state"
        )

        report = handoff_lint.lint(pinned)

        self.assertTrue(report.ok())
        self.assertFalse(report.ok(strict=True))
        self.assertEqual(["abcdef1"], handoff_lint.find_pinned_tips(pinned))
        self.assertTrue(any("pinned current tip hash" in item for item in report.warnings))

    def test_pinned_sha256_tip_is_detected(self) -> None:
        value = "a" * 64
        pinned = VALID_HANDOFF.replace(
            "Verify the live state", f"Current HEAD is {value}. Verify the live state"
        )

        self.assertEqual([value], handoff_lint.find_pinned_tips(pinned))
        self.assertFalse(handoff_lint.lint(pinned).ok(strict=True))

    def test_historical_commit_hash_is_not_treated_as_current_tip(self) -> None:
        historical = VALID_HANDOFF.replace(
            "Added a portable, read-only state collector.",
            "Commit abcdef1 added a portable, read-only state collector.",
        )

        report = handoff_lint.lint(historical)

        self.assertEqual([], handoff_lint.find_pinned_tips(historical))
        self.assertTrue(report.ok(strict=True), report.to_dict())

    def test_missing_commit_and_push_guards_are_reported(self) -> None:
        unguarded = VALID_HANDOFF.replace(
            "Do not commit unless explicitly requested and project policy permits it.\n"
            "Do not push; pushing is not authorized.\n",
            "Keep changes safe and reversible.\n",
        )

        report = handoff_lint.lint(
            unguarded, branch="feature/reminders", default_branch="main"
        )

        self.assertFalse(report.ok(strict=True))
        self.assertTrue(
            any("no commit-authorization guard" in item for item in report.warnings)
        )
        self.assertTrue(any("no push guard" in item for item in report.warnings))
        self.assertTrue(
            any("differs from default" in item for item in report.warnings),
            report.warnings,
        )

    def test_strict_cli_exit_codes_distinguish_valid_invalid_and_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid_file = root / "valid.md"
            valid_file.write_text(VALID_HANDOFF, encoding="utf-8")
            invalid_file = root / "invalid.md"
            invalid_file.write_text("# Read first\nOnly one section.\n", encoding="utf-8")

            valid = run_lint_cli("check", "--file", str(valid_file), "--strict")
            invalid = run_lint_cli("check", "--file", str(invalid_file), "--strict")
            missing = run_lint_cli(
                "check", "--file", str(root / "missing.md"), "--strict"
            )

        self.assertEqual(0, valid.returncode, valid.stdout + valid.stderr)
        self.assertIn("OK (strict)", valid.stdout)
        self.assertEqual(1, invalid.returncode)
        self.assertIn("FAILED (strict)", invalid.stdout)
        self.assertEqual(2, missing.returncode)
        self.assertIn("cannot read", missing.stderr)

    def test_warning_only_document_passes_standard_cli_but_fails_strict_cli(self) -> None:
        pinned = VALID_HANDOFF.replace(
            "Verify the live state", "Current tip: abcdef1. Verify the live state"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            handoff_file = Path(temp_dir) / "HANDOFF.md"
            handoff_file.write_text(pinned, encoding="utf-8")
            standard = run_lint_cli("check", "--file", str(handoff_file))
            strict = run_lint_cli(
                "check", "--file", str(handoff_file), "--strict", "--json"
            )

        self.assertEqual(0, standard.returncode)
        self.assertEqual(1, strict.returncode)
        payload = json.loads(strict.stdout)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["strict_ok"])
        self.assertFalse(payload["selected_ok"])


class PromptTests(unittest.TestCase):
    def test_valid_prompt_schema_and_default_verification_text(self) -> None:
        spec = valid_prompt_spec()

        self.assertEqual([], handoff_lint.validate_prompt_spec(spec))
        rendered = handoff_lint.render_pickup_prompt(**spec)

        self.assertIn("# Continue: Portable session handoff", rendered)
        self.assertIn("If Git exists, re-derive", rendered)
        self.assertIn("treat all last-known numbers below as stale", rendered)
        self.assertIn("## Open threads — pick one", rendered)

    def test_prompt_schema_rejects_missing_lists_and_mutation_guards(self) -> None:
        spec = valid_prompt_spec()
        spec["read_order"] = []
        spec["constraints"] = ["Be careful."]

        errors = handoff_lint.validate_prompt_spec(spec)

        self.assertIn("prompt specification requires a non-empty 'read_order' list", errors)
        self.assertTrue(any("no commit-authorization guard" in item for item in errors))
        self.assertTrue(any("no push guard" in item for item in errors))

    def test_prompt_schema_rejects_blank_scalar_list_items(self) -> None:
        spec = valid_prompt_spec()
        spec["state_lines"] = [""]
        spec["accomplishments"] = [None]
        spec["verify_note"] = []

        errors = handoff_lint.validate_prompt_spec(spec)

        self.assertIn("state_lines[0] must be a non-empty string", errors)
        self.assertIn("accomplishments[0] must be a non-empty string", errors)
        self.assertIn("'verify_note' must be a non-empty string when supplied", errors)

    def test_custom_verify_note_cannot_replace_fixed_safety_text(self) -> None:
        spec = valid_prompt_spec()
        spec["verify_note"] = "Also inspect the package identity."

        rendered = handoff_lint.render_pickup_prompt(**spec)

        self.assertIn("If Git exists, re-derive", rendered)
        self.assertIn("Also inspect the package identity.", rendered)

    def test_prompt_cli_rejects_current_head_pin(self) -> None:
        spec = valid_prompt_spec()
        spec["state_lines"] = ["Current HEAD is abcdef1."]

        with tempfile.TemporaryDirectory() as temp_dir:
            spec_file = Path(temp_dir) / "prompt.json"
            spec_file.write_text(json.dumps(spec), encoding="utf-8")
            result = run_lint_cli("prompt", "--spec", str(spec_file))

        self.assertEqual(2, result.returncode)
        self.assertIn("rendered prompt pins current tip hash(es): abcdef1", result.stderr)
        self.assertEqual("", result.stdout)

    def test_prompt_cli_success_and_invalid_json_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            valid_file = root / "valid.json"
            valid_file.write_text(json.dumps(valid_prompt_spec()), encoding="utf-8")
            invalid_file = root / "invalid.json"
            invalid_file.write_text("{not-json", encoding="utf-8")

            valid = run_lint_cli("prompt", "--spec", str(valid_file))
            invalid = run_lint_cli("prompt", "--spec", str(invalid_file))

        self.assertEqual(0, valid.returncode, valid.stderr)
        self.assertIn("# Continue: Portable session handoff", valid.stdout)
        self.assertIn("If Git exists, re-derive", valid.stdout)
        self.assertEqual(2, invalid.returncode)
        self.assertIn("invalid JSON", invalid.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
