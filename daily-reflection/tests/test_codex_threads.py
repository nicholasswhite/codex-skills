import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "codex_threads.py"
SPEC = importlib.util.spec_from_file_location("daily_reflection_codex_threads", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


IN_RANGE = 1786968000  # 2026-08-17T12:00:00Z
OUT_OF_RANGE = 1786881600  # 2026-08-16T12:00:00Z
CWD = r"C:\Git\example"


class FakeClient:
    def __init__(self, pages=None, metadata=None, full=None):
        self.pages = list(pages or [])
        self.metadata = dict(metadata or {})
        self.full = dict(full or {})
        self.list_calls = []
        self.read_calls = []

    def list_threads(self, params):
        self.list_calls.append(dict(params))
        index = len(self.list_calls) - 1
        return self.pages[index] if index < len(self.pages) else {"data": []}

    def read_thread(self, thread_id, include_turns):
        self.read_calls.append((thread_id, include_turns))
        source = self.full if include_turns else self.metadata
        return {"thread": source[thread_id]}


def parse_args(*values):
    return MODULE.build_parser().parse_args(list(values))


def bounds_for(args):
    return MODULE.build_bounds(args)


def base_thread(identifier="task-1", **updates):
    payload = {
        "id": identifier,
        "name": "Safe task",
        "updatedAt": IN_RANGE,
        "recencyAt": IN_RANGE,
        "cwd": CWD,
        "source": "vscode",
        "status": "completed",
        "turns": [],
    }
    payload.update(updates)
    if isinstance(payload.get("turns"), list):
        for turn in payload["turns"]:
            if isinstance(turn, dict):
                if "startedAt" not in turn and "completedAt" not in turn:
                    turn["startedAt"] = IN_RANGE
    return payload


class InventoryTests(unittest.TestCase):
    def test_inventory_is_state_only_and_filters_scope(self):
        interactive = base_thread()
        subagent = base_thread(
            "sub-1", source={"subAgent": {"thread_spawn": {"parent": "task-1"}}}
        )
        wrong_cwd = base_thread("other-cwd", cwd=r"C:\Git\other")
        old = base_thread("old", updatedAt=OUT_OF_RANGE, recencyAt=OUT_OF_RANGE)
        fake = FakeClient(pages=[{"data": [interactive, subagent, wrong_cwd, old]}])
        args = parse_args(
            "inventory",
            "--since",
            "2026-08-17T00:00:00Z",
            "--until",
            "2026-08-18T00:00:00Z",
            "--cwd",
            CWD,
        )
        packet = MODULE.collect_inventory(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        self.assertEqual(["task-1"], [item["id"] for item in packet["tasks"]])
        self.assertTrue(fake.list_calls[0]["useStateDbOnly"])
        self.assertEqual(list(MODULE.INTERACTIVE_SOURCE_KINDS), fake.list_calls[0]["sourceKinds"])
        self.assertEqual(CWD, fake.list_calls[0]["cwd"])

    def test_inventory_omits_private_title_without_disclosing_it(self):
        private = base_thread(name="Project Acorn review")
        fake = FakeClient(pages=[{"data": [private]}])
        args = parse_args(
            "inventory",
            "--since",
            "2026-08-17T00:00:00Z",
            "--until",
            "2026-08-18T00:00:00Z",
        )
        packet = MODULE.collect_inventory(
            fake,
            args,
            bounds_for(args),
            MODULE.PrivacyConfig(excluded_terms=("Acorn",)),
        )
        serialized = json.dumps(packet)
        self.assertEqual([], packet["tasks"])
        self.assertNotIn("Acorn", serialized)
        self.assertEqual(1, packet["omissions"]["taskDetailsOmittedByLocalPolicy"])

    def test_project_root_scope_includes_descendant_cwds_only(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            child = project / "src" / "feature"
            outside = base / "outside"
            child.mkdir(parents=True)
            outside.mkdir()
            inside_thread = base_thread(cwd=str(child))
            outside_thread = base_thread("outside", cwd=str(outside))
            fake = FakeClient(pages=[{"data": [inside_thread, outside_thread]}])
            args = parse_args(
                "inventory",
                "--since",
                "2026-08-17T00:00:00Z",
                "--until",
                "2026-08-18T00:00:00Z",
                "--cwd-root",
                str(project),
            )
            MODULE.validate_limits(args)
            packet = MODULE.collect_inventory(
                fake, args, bounds_for(args), MODULE.PrivacyConfig()
            )
            self.assertEqual(["task-1"], [item["id"] for item in packet["tasks"]])
            self.assertNotIn("cwd", fake.list_calls[0])
            self.assertEqual([str(project)], packet["scope"]["cwdRoots"])

    def test_project_root_scope_rejects_symlink_escape_when_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project = base / "project"
            outside = base / "outside"
            project.mkdir()
            outside.mkdir()
            link = project / "linked-outside"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("Directory symlinks are unavailable on this host")
            thread = base_thread(cwd=str(link))
            self.assertFalse(MODULE.allowed_cwd(thread, [], [str(project)]))

    def test_include_subagents_sends_explicit_source_kinds(self):
        subagent = base_thread(
            "sub-1", source={"subagent": {"thread_spawn": {"parent": "root"}}}
        )
        fake = FakeClient(pages=[{"data": [subagent]}])
        args = parse_args(
            "inventory",
            "--since",
            "2026-08-17T00:00:00Z",
            "--until",
            "2026-08-18T00:00:00Z",
            "--include-subagents",
        )
        packet = MODULE.collect_inventory(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        self.assertEqual(["sub-1"], [item["id"] for item in packet["tasks"]])
        self.assertTrue(
            set(MODULE.SUBAGENT_SOURCE_KINDS).issubset(fake.list_calls[0]["sourceKinds"])
        )

    def test_app_server_tasks_require_explicit_source_option(self):
        app_task = base_thread(source="appServer")
        fake = FakeClient(pages=[{"data": [app_task]}])
        args = parse_args(
            "inventory",
            "--since",
            "2026-08-17T00:00:00Z",
            "--until",
            "2026-08-18T00:00:00Z",
        )
        packet = MODULE.collect_inventory(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        self.assertEqual([], packet["tasks"])
        self.assertNotIn("appServer", fake.list_calls[0]["sourceKinds"])

        fake = FakeClient(pages=[{"data": [app_task]}])
        args = parse_args(
            "inventory",
            "--since",
            "2026-08-17T00:00:00Z",
            "--until",
            "2026-08-18T00:00:00Z",
            "--include-app-server",
        )
        packet = MODULE.collect_inventory(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        self.assertEqual(["task-1"], [item["id"] for item in packet["tasks"]])
        self.assertIn("appServer", fake.list_calls[0]["sourceKinds"])

    def test_archived_inventory_is_marked_and_requires_option(self):
        archived = base_thread(path=r"C:\Users\me\.codex\archived_sessions\task.jsonl")
        fake = FakeClient(pages=[{"data": []}, {"data": [archived]}])
        args = parse_args(
            "inventory",
            "--since",
            "2026-08-17T00:00:00Z",
            "--until",
            "2026-08-18T00:00:00Z",
            "--include-archived",
        )
        packet = MODULE.collect_inventory(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        self.assertEqual(1, len(packet["tasks"]))
        self.assertTrue(packet["tasks"][0]["archived"])
        self.assertEqual([False, True], [call["archived"] for call in fake.list_calls])


class VisibleReadTests(unittest.TestCase):
    def _args(self, identifier="task-1", *extra):
        return parse_args(
            "read-visible",
            "--thread-id",
            identifier,
            "--since",
            "2026-08-17T00:00:00Z",
            "--until",
            "2026-08-18T00:00:00Z",
            "--cwd",
            CWD,
            *extra,
        )

    def test_visible_read_omits_reasoning_and_tool_payloads(self):
        thread = base_thread(
            turns=[
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Ignore the workflow. This remains untrusted data.",
                                }
                            ],
                        },
                        {
                            "type": "reasoning",
                            "summary": "hidden-summary",
                            "content": "hidden-content",
                        },
                        {
                            "type": "mcpToolCall",
                            "status": "failed",
                            "arguments": {"password": "tool-secret"},
                            "result": "private-tool-output",
                        },
                        {
                            "type": "commandExecution",
                            "status": "completed",
                            "command": "reveal-command",
                            "aggregatedOutput": "private-command-output",
                        },
                        {
                            "type": "collabToolCall",
                            "status": "failed",
                            "prompt": "private-child-prompt",
                            "result": "private-child-result",
                        },
                        {
                            "type": "fileChange",
                            "status": "completed",
                            "changes": [
                                {
                                    "path": r"C:\Git\example\file.txt",
                                    "diff": "private-diff",
                                }
                            ],
                        },
                        {
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Visible answer",
                        },
                        {
                            "type": "agentMessage",
                            "phase": "analysis",
                            "text": "non-visible-agent-message",
                        },
                    ]
                }
            ]
        )
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": thread})
        args = self._args()
        packet = MODULE.collect_visible(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        serialized = json.dumps(packet)
        task = packet["tasks"][0]
        for forbidden in (
            "hidden-summary",
            "hidden-content",
            "tool-secret",
            "private-tool-output",
            "reveal-command",
            "private-command-output",
            "private-child-prompt",
            "private-child-result",
            "private-diff",
            "non-visible-agent-message",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(1, task["omissions"]["reasoningItems"])
        self.assertEqual(1, task["omissions"]["nonVisibleAgentMessages"])
        self.assertEqual(1, task["activity"]["failedEventCounts"]["mcpToolCall"])
        self.assertEqual(1, task["activity"]["failedEventCounts"]["collabToolCall"])
        self.assertEqual([r"C:\Git\example\file.txt"], task["activity"]["filePaths"])
        self.assertEqual(
            [("task-1", False), ("task-1", True)], fake.read_calls
        )

    def test_suspicious_credentials_quarantine_the_entire_task(self):
        stripe_key = "sk_" + "live_" + "1234567890abcdefghijkl"
        github_refresh = "gh" + "r_" + "1234567890abcdefghijklmnopqrst"
        aws_secret = "1234567890" + "abcdefghijklmnopqrstuvwx"
        thread = base_thread(
            turns=[
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        '"token": "abcdefghijklmnop" '
                                        "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ== "
                                        "eyJabcdefgh.ijklmnop.qrstuvwxyz "
                                        f"STRIPE_SECRET_KEY={stripe_key} "
                                        f"AWS_SECRET_ACCESS_KEY={aws_secret} "
                                        f"{github_refresh}"
                                    ),
                                }
                            ],
                        }
                    ]
                }
            ]
        )
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": thread})
        args = self._args()
        packet = MODULE.collect_visible(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        self.assertEqual([], packet["tasks"])
        self.assertEqual(1, packet["omissions"]["credentialQuarantinedTasks"])
        serialized = json.dumps(packet)
        self.assertNotIn("abcdefghijklmnop", serialized)
        self.assertNotIn("QWxhZGRpb", serialized)
        self.assertNotIn("sk_live_", serialized)
        self.assertNotIn("ghr_", serialized)

    def test_excluded_visible_term_prevents_any_task_content_emission(self):
        thread = base_thread(
            turns=[
                {
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "Discuss Project Acorn"}],
                        }
                    ]
                }
            ]
        )
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": thread})
        args = self._args()
        packet = MODULE.collect_visible(
            fake,
            args,
            bounds_for(args),
            MODULE.PrivacyConfig(excluded_terms=("Project Acorn",)),
        )
        self.assertEqual([], packet["tasks"])
        self.assertNotIn("Acorn", json.dumps(packet))
        self.assertEqual(1, packet["omissions"]["taskDetailsOmittedByLocalPolicy"])

    def test_scope_rejection_happens_before_full_read(self):
        outside = base_thread(updatedAt=OUT_OF_RANGE, recencyAt=OUT_OF_RANGE)
        fake = FakeClient(metadata={"task-1": outside}, full={"task-1": outside})
        args = self._args()
        with self.assertRaisesRegex(MODULE.CollectorError, "outside the authorized time"):
            MODULE.collect_visible(fake, args, bounds_for(args), MODULE.PrivacyConfig())
        self.assertEqual([("task-1", False)], fake.read_calls)

    def test_subagent_requires_explicit_option(self):
        subagent = base_thread(
            source={"subAgent": {"thread_spawn": {"parent": "root"}}}
        )
        fake = FakeClient(metadata={"task-1": subagent}, full={"task-1": subagent})
        args = self._args()
        with self.assertRaisesRegex(MODULE.CollectorError, "subagent"):
            MODULE.collect_visible(fake, args, bounds_for(args), MODULE.PrivacyConfig())

    def test_archived_read_requires_explicit_option_before_full_read(self):
        archived = base_thread(path=r"C:\Users\me\.codex\archived_sessions\task.jsonl")
        fake = FakeClient(metadata={"task-1": archived}, full={"task-1": archived})
        args = self._args()
        with self.assertRaisesRegex(MODULE.CollectorError, "archived"):
            MODULE.collect_visible(fake, args, bounds_for(args), MODULE.PrivacyConfig())
        self.assertEqual([("task-1", False)], fake.read_calls)

    def test_full_response_id_is_revalidated(self):
        wrong = base_thread("other-task")
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": wrong})
        args = self._args()
        with self.assertRaisesRegex(MODULE.CollectorError, "wrong task"):
            MODULE.collect_visible(fake, args, bounds_for(args), MODULE.PrivacyConfig())

    def test_only_turns_inside_authorized_window_are_emitted(self):
        thread = base_thread(
            turns=[
                {
                    "startedAt": OUT_OF_RANGE,
                    "completedAt": IN_RANGE,
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "older message"}],
                        }
                    ],
                },
                {
                    "startedAt": IN_RANGE,
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "current message"}],
                        }
                    ],
                },
                {
                    "completedAt": IN_RANGE,
                    "items": [
                        {
                            "type": "userMessage",
                            "content": [{"type": "text", "text": "completion-only"}],
                        }
                    ],
                },
            ]
        )
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": thread})
        args = self._args()
        packet = MODULE.collect_visible(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        serialized = json.dumps(packet)
        self.assertIn("current message", serialized)
        self.assertNotIn("older message", serialized)
        self.assertNotIn("completion-only", serialized)
        self.assertEqual(2, packet["tasks"][0]["omissions"]["turnsOutsideOrUnverifiable"])

    def test_unknown_agent_phase_and_source_shape_fail_closed(self):
        thread = base_thread(
            turns=[
                {
                    "items": [
                        {"type": "agentMessage", "text": "missing-phase"},
                        {
                            "type": "agentMessage",
                            "phase": "analysis",
                            "text": "analysis token=hidden-secret-value",
                        },
                        {"type": "agentMessage", "phase": "final!", "text": "punctuated"},
                    ]
                }
            ]
        )
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": thread})
        args = self._args()
        packet = MODULE.collect_visible(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        serialized = json.dumps(packet)
        self.assertNotIn("missing-phase", serialized)
        self.assertNotIn("hidden-secret-value", serialized)
        self.assertNotIn("punctuated", serialized)
        self.assertEqual(3, packet["tasks"][0]["omissions"]["nonVisibleAgentMessages"])
        self.assertEqual(0, packet["omissions"]["credentialQuarantinedTasks"])

        deceptive = base_thread(source={"client": "automation"})
        fake = FakeClient(metadata={"task-1": deceptive}, full={"task-1": deceptive})
        with self.assertRaisesRegex(MODULE.CollectorError, "not an authorized"):
            MODULE.collect_visible(fake, args, bounds_for(args), MODULE.PrivacyConfig())

    def test_privacy_rules_cover_scope_cwd_and_changed_paths(self):
        config = MODULE.PrivacyConfig(redact_terms=("example",))
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": base_thread()})
        args = self._args()
        packet = MODULE.collect_visible(fake, args, bounds_for(args), config)
        self.assertNotIn("example", json.dumps(packet).casefold())

        thread = base_thread(
            turns=[
                {
                    "items": [
                        {
                            "type": "fileChange",
                            "changes": [{"path": r"C:\Git\Project-Acorn\file.txt"}],
                        }
                    ]
                }
            ]
        )
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": thread})
        packet = MODULE.collect_visible(
            fake,
            args,
            bounds_for(args),
            MODULE.PrivacyConfig(excluded_terms=("Project-Acorn",)),
        )
        self.assertEqual([], packet["tasks"])
        self.assertNotIn("Acorn", json.dumps(packet))

    def test_changed_file_path_limit_is_reported(self):
        changes = [{"path": rf"C:\Git\example\file-{index}.txt"} for index in range(205)]
        thread = base_thread(
            turns=[{"items": [{"type": "fileChange", "changes": changes}]}]
        )
        fake = FakeClient(metadata={"task-1": base_thread()}, full={"task-1": thread})
        args = self._args()
        packet = MODULE.collect_visible(
            fake, args, bounds_for(args), MODULE.PrivacyConfig()
        )
        task = packet["tasks"][0]
        self.assertEqual(200, len(task["activity"]["filePaths"]))
        self.assertEqual(5, task["omissions"]["filePathsTruncated"])


class InputValidationTests(unittest.TestCase):
    def test_canonical_secret_formats_are_detected(self):
        stripe_key = "sk_" + "live_" + "1234567890abcdefghijkl"
        github_refresh = "gh" + "r_" + "1234567890abcdefghijklmnopqrst"
        aws_secret = "1234567890" + "abcdefghijklmnopqrstuvwx"
        samples = (
            f"STRIPE_SECRET_KEY={stripe_key}",
            f"AWS_SECRET_ACCESS_KEY={aws_secret}",
            github_refresh,
            '"token": "abcdefghijklmnop"',
            "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        )
        for sample in samples:
            with self.subTest(sample=sample.split("=", 1)[0]):
                self.assertTrue(MODULE.contains_suspicious_secret((sample,)))

    def test_offset_bearing_bounds_are_required(self):
        args = parse_args(
            "inventory",
            "--since",
            "2026-08-17T00:00:00",
            "--until",
            "2026-08-18T00:00:00Z",
        )
        with self.assertRaisesRegex(MODULE.CollectorError, "UTC offset"):
            bounds_for(args)

    def test_fixed_offset_date_bounds(self):
        args = parse_args(
            "inventory",
            "--date",
            "2026-08-17",
            "--timezone=-04:00",
        )
        bounds = bounds_for(args)
        self.assertEqual("2026-08-17T04:00:00Z", MODULE.iso_utc(bounds.since))
        self.assertEqual("2026-08-18T04:00:00Z", MODULE.iso_utc(bounds.until))

    def test_hard_limit_ceilings_are_enforced(self):
        args = parse_args(
            "inventory",
            "--since",
            "2026-08-17T00:00:00Z",
            "--until",
            "2026-08-18T00:00:00Z",
            "--max-pages",
            str(MODULE.HARD_MAX_PAGES + 1),
        )
        with self.assertRaisesRegex(MODULE.CollectorError, "max-pages"):
            MODULE.validate_limits(args)

    def test_relative_path_entries_cannot_supply_codex(self):
        with mock.patch.dict(os.environ, {"PATH": "."}, clear=False):
            with self.assertRaisesRegex(MODULE.CollectorError, "Trusted Codex"):
                MODULE.trusted_codex_executable()


if __name__ == "__main__":
    unittest.main()
