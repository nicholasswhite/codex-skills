import importlib.util
import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reflection_store.py"
SPEC = importlib.util.spec_from_file_location("daily_reflection_store", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


NOW = datetime(2026, 8, 17, 20, 44, 55, 123456, tzinfo=timezone.utc)
PROJECT_ROOT = r"C:\Git\Example"


def valid_payload(**overrides):
    payload = {
        "schemaVersion": 1,
        "reflectionDate": "2026-08-17",
        "timezone": "America/New_York",
        "reportMarkdown": (
            "# Daily Reflection\n\n"
            "## Scope and omissions\n\nCurrent project, today.\n\n"
            "## Applied local changes\n\nOne stale path was corrected and validated.\n\n"
            "## Durable records\n\nThis synthesized report was saved locally.\n"
        ),
        "friction": [
            {
                "category": "skill-error",
                "skill": "example-skill",
                "severity": "dated-doc",
                "summary": "A documented local path was stale.",
                "impact": "The first invocation failed before the path was corrected.",
                "confidence": "high",
                "scope": "skill",
            }
        ],
        "taskCheckpoints": [
            {"id": "task-1", "updatedAt": "2026-08-17T20:30:00Z"}
        ],
    }
    payload.update(overrides)
    return payload


class ReflectionStoreTests(unittest.TestCase):
    def test_commit_writes_report_friction_and_atomic_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"
            receipt = MODULE.commit(
                valid_payload(),
                project_root=PROJECT_ROOT,
                data_root=root,
                now=NOW,
            )

            self.assertTrue(receipt["ok"])
            report = root / receipt["report"]["path"]
            self.assertTrue(report.is_file())
            self.assertIn("# Daily Reflection", report.read_text(encoding="utf-8"))

            friction_rows = [
                json.loads(line)
                for line in (root / "friction.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(1, len(friction_rows))
            self.assertEqual("dated-doc", friction_rows[0]["severity"])

            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(1, state["generation"])
            project = state["projects"][receipt["state"]["projectKey"]]
            self.assertEqual("2026-08-17T20:30:00Z", project["taskCheckpoints"]["task-1"])

            serialized = json.dumps(state) + json.dumps(friction_rows)
            self.assertNotIn("Daily Reflection", serialized)
            self.assertNotIn(r"C:\Git\Example", serialized)
            self.assertNotIn('"label"', json.dumps(state))

    def test_write_order_is_report_then_friction_then_state(self):
        with tempfile.TemporaryDirectory() as directory:
            order = []
            original_report = MODULE._write_report
            original_friction = MODULE._append_friction
            original_state = MODULE._atomic_write_json

            def report(*args, **kwargs):
                order.append("report")
                return original_report(*args, **kwargs)

            def friction(*args, **kwargs):
                order.append("friction")
                return original_friction(*args, **kwargs)

            def state(*args, **kwargs):
                order.append("state")
                return original_state(*args, **kwargs)

            with mock.patch.object(MODULE, "_write_report", side_effect=report), mock.patch.object(
                MODULE, "_append_friction", side_effect=friction
            ), mock.patch.object(MODULE, "_atomic_write_json", side_effect=state):
                MODULE.commit(
                    valid_payload(),
                    project_root=PROJECT_ROOT,
                    data_root=Path(directory),
                    now=NOW,
                )
            self.assertEqual(["report", "friction", "state"], order)

    def test_concurrent_commits_are_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"
            worker = r'''
import importlib.util
import json
from datetime import datetime
from pathlib import Path
import sys
import time

spec = importlib.util.spec_from_file_location("reflection_store_worker", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = module._write_report
def slow_write(*args, **kwargs):
    time.sleep(0.35)
    return original(*args, **kwargs)
module._write_report = slow_write
module.commit(
    json.loads(sys.argv[3]),
    project_root=sys.argv[4],
    data_root=Path(sys.argv[2]),
    now=datetime.fromisoformat(sys.argv[5]),
)
'''
            payload = json.dumps(valid_payload())
            commands = []
            for timestamp in (
                "2026-08-17T20:44:55.123456+00:00",
                "2026-08-17T20:44:56.123456+00:00",
            ):
                commands.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            worker,
                            str(SCRIPT),
                            str(root),
                            payload,
                            PROJECT_ROOT,
                            timestamp,
                        ],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            results = [process.communicate(timeout=15) for process in commands]
            for process, result in zip(commands, results):
                self.assertEqual(0, process.returncode, result[1])
            state = json.loads((root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(2, state["generation"])
            self.assertEqual(
                2,
                len((root / "friction.jsonl").read_text(encoding="utf-8").splitlines()),
            )

    def test_second_commit_never_overwrites_and_increments_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = MODULE.commit(
                valid_payload(), project_root=PROJECT_ROOT, data_root=root, now=NOW
            )
            second = MODULE.commit(
                valid_payload(), project_root=PROJECT_ROOT, data_root=root, now=NOW
            )
            self.assertNotEqual(first["report"]["path"], second["report"]["path"])
            self.assertNotEqual(first["report"]["id"], second["report"]["id"])
            self.assertTrue((root / first["report"]["path"]).is_file())
            self.assertTrue((root / second["report"]["path"]).is_file())
            self.assertEqual(2, second["state"]["generation"])
            event_ids = [
                json.loads(line)["eventId"]
                for line in (root / "friction.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(event_ids), len(set(event_ids)))

    def test_report_failure_leaves_friction_and_state_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                MODULE,
                "_write_report",
                side_effect=MODULE.StorageError("Could not save the reflection report."),
            ):
                with self.assertRaises(MODULE.StorageError) as caught:
                    MODULE.commit(
                        valid_payload(), project_root=PROJECT_ROOT, data_root=root, now=NOW
                    )
            self.assertFalse(caught.exception.report_saved)
            self.assertFalse((root / "friction.jsonl").exists())
            self.assertFalse((root / "state.json").exists())

    def test_atomic_report_publication_failure_leaves_no_partial_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(MODULE.os, "link", side_effect=OSError("link failed")):
                with self.assertRaises(MODULE.StorageError) as caught:
                    MODULE.commit(
                        valid_payload(),
                        project_root=PROJECT_ROOT,
                        data_root=root,
                        now=NOW,
                    )
            self.assertFalse(caught.exception.report_saved)
            reports = list((root / "reflections").rglob("*.md"))
            temporaries = list((root / "reflections").rglob(".report-*.tmp"))
            self.assertEqual([], reports)
            self.assertEqual([], temporaries)

    def test_friction_failure_after_report_does_not_advance_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = MODULE.commit(
                valid_payload(friction=[]),
                project_root=PROJECT_ROOT,
                data_root=root,
                now=NOW,
            )
            state_before = (root / "state.json").read_bytes()
            later = datetime(2026, 8, 17, 21, 0, tzinfo=timezone.utc)
            with mock.patch.object(
                MODULE,
                "_append_friction",
                side_effect=MODULE.StorageError("Could not append the reflection friction log."),
            ):
                with self.assertRaises(MODULE.StorageError) as caught:
                    MODULE.commit(
                        valid_payload(),
                        project_root=PROJECT_ROOT,
                        data_root=root,
                        now=later,
                    )
            self.assertTrue(caught.exception.report_saved)
            self.assertFalse(caught.exception.state_advanced)
            self.assertIn("report", caught.exception.partial_receipt)
            self.assertEqual(state_before, (root / "state.json").read_bytes())
            self.assertTrue((root / first["report"]["path"]).exists())

    def test_state_replace_failure_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.commit(
                valid_payload(friction=[]),
                project_root=PROJECT_ROOT,
                data_root=root,
                now=NOW,
            )
            state_before = (root / "state.json").read_bytes()
            with mock.patch.object(
                MODULE,
                "_atomic_write_json",
                side_effect=MODULE.StorageError("Could not atomically advance reflection state."),
            ):
                with self.assertRaises(MODULE.StorageError) as caught:
                    MODULE.commit(
                        valid_payload(friction=[]),
                        project_root=PROJECT_ROOT,
                        data_root=root,
                        now=datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc),
                    )
            self.assertTrue(caught.exception.report_saved)
            self.assertIn("report", caught.exception.partial_receipt)
            self.assertEqual(state_before, (root / "state.json").read_bytes())

    def test_atomic_friction_failure_preserves_existing_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "friction.jsonl"
            original = b'{"existing":true}\n'
            path.write_bytes(original)
            with mock.patch.object(MODULE.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(MODULE.StorageError):
                    MODULE._append_friction(path, [{"new": True}])
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([], list(root.glob(".friction-*.tmp")))

    def test_corrupt_state_fails_before_any_report_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"
            root.mkdir()
            (root / "state.json").write_text("not-json", encoding="utf-8")
            with self.assertRaises(MODULE.StorageError):
                MODULE.commit(
                    valid_payload(), project_root=PROJECT_ROOT, data_root=root, now=NOW
                )
            self.assertFalse((root / "reflections").exists())
            self.assertFalse((root / "friction.jsonl").exists())

    def test_nested_state_schema_is_validated_before_status_or_commit(self):
        malformed_states = (
            {
                "schemaVersion": 1,
                "generation": 1,
                "lastSuccessfulAt": "2026-08-17T20:00:00Z",
                "lastReport": "reflections/2026-08-17/reflection-safe.md",
                "projects": {"a" * 24: "not-an-object"},
            },
            {
                "schemaVersion": 1,
                "generation": 1,
                "lastSuccessfulAt": "2026-08-17T20:00:00Z",
                "lastReport": "reflections/2026-08-17/reflection-safe.md",
                "projects": {
                    "a" * 24: {
                        "lastSuccessfulAt": "2026-08-17T20:00:00Z",
                        "lastReport": "reflections/2026-08-17/reflection-safe.md",
                        "taskCheckpoints": [],
                    }
                },
            },
        )
        for malformed in malformed_states:
            with self.subTest(project=malformed["projects"]):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory) / "daily-reflection"
                    root.mkdir()
                    (root / "state.json").write_text(
                        json.dumps(malformed), encoding="utf-8"
                    )
                    with self.assertRaises(MODULE.StorageError):
                        MODULE.status(project_root=PROJECT_ROOT, data_root=root)
                    with self.assertRaises(MODULE.StorageError):
                        MODULE.commit(
                            valid_payload(),
                            project_root=PROJECT_ROOT,
                            data_root=root,
                            now=NOW,
                        )
                    self.assertFalse((root / "reflections").exists())

    def test_linked_destination_component_is_rejected_before_report_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"
            (root / "reflections").mkdir(parents=True)

            def looks_linked(path):
                return Path(path).name == "reflections"

            with mock.patch.object(MODULE, "_is_link_or_junction", side_effect=looks_linked):
                with self.assertRaisesRegex(MODULE.StorageError, "component is unsafe"):
                    MODULE.commit(
                        valid_payload(), project_root=PROJECT_ROOT, data_root=root, now=NOW
                    )
            self.assertFalse((root / "state.json").exists())
            self.assertFalse((root / "friction.jsonl").exists())

    def test_linked_friction_log_is_rejected_without_advancing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"

            original = MODULE._is_link_or_junction

            def linked_friction(path):
                return Path(path).name == "friction.jsonl" or original(path)

            with mock.patch.object(MODULE, "_is_link_or_junction", side_effect=linked_friction):
                with self.assertRaises(MODULE.StorageError) as caught:
                    MODULE.commit(
                        valid_payload(), project_root=PROJECT_ROOT, data_root=root, now=NOW
                    )
            self.assertTrue(caught.exception.report_saved)
            self.assertFalse(caught.exception.state_advanced)
            self.assertFalse((root / "state.json").exists())

    def test_project_contained_data_root_is_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            root = project / ".codex" / "daily-reflection"
            with self.assertRaisesRegex(MODULE.StorageError, "inside the active project"):
                MODULE.commit(
                    valid_payload(),
                    project_root=str(project),
                    data_root=root,
                    now=NOW,
                )
            self.assertFalse(root.exists())
            with self.assertRaisesRegex(MODULE.StorageError, "inside the active project"):
                MODULE.status(project_root=str(project), data_root=root)

    def test_real_linked_data_root_is_rejected_when_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "target"
            target.mkdir()
            link = base / "linked-root"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("Directory symlinks are unavailable on this host")
            with self.assertRaisesRegex(MODULE.StorageError, "link or junction"):
                MODULE.commit(
                    valid_payload(), project_root=PROJECT_ROOT, data_root=link, now=NOW
                )

    def test_unknown_fields_raw_packets_and_credentials_are_rejected(self):
        cases = (
            valid_payload(unexpected=True),
            valid_payload(
                reportMarkdown=(
                    "## Scope and omissions\n## Applied local changes\n"
                    '{"operation": "read-visible", "messages": []}'
                )
            ),
            valid_payload(
                reportMarkdown=(
                    "## Scope and omissions\n## Applied local changes\n"
                    "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=="
                )
            ),
            valid_payload(
                friction=[
                    {
                        "category": "other",
                        "severity": "observation",
                        "summary": "Safe summary",
                        "confidence": "low",
                        "scope": "task",
                        "rawMessages": [],
                    }
                ]
            ),
        )
        for payload in cases:
            with self.subTest(payload=list(payload)):
                with self.assertRaises(MODULE.ValidationError):
                    MODULE.validate_envelope(payload)

        for checkpoints in (
            [
                {"id": "same", "updatedAt": "2026-08-17T20:00:00Z"},
                {"id": "same", "updatedAt": "2026-08-17T21:00:00Z"},
            ],
            [
                {
                    "id": "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
                    "updatedAt": "2026-08-17T20:00:00Z",
                }
            ],
        ):
            with self.subTest(checkpoints=checkpoints):
                with self.assertRaises(MODULE.ValidationError):
                    MODULE.validate_envelope(valid_payload(taskCheckpoints=checkpoints))

    def test_checkpoint_timestamps_only_advance_monotonically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            MODULE.commit(
                valid_payload(
                    friction=[],
                    taskCheckpoints=[
                        {"id": "task-1", "updatedAt": "2026-08-17T21:00:00Z"}
                    ],
                ),
                project_root=PROJECT_ROOT,
                data_root=root,
                now=NOW,
            )
            MODULE.commit(
                valid_payload(
                    friction=[],
                    taskCheckpoints=[
                        {"id": "task-1", "updatedAt": "2026-08-17T20:00:00Z"}
                    ],
                ),
                project_root=PROJECT_ROOT,
                data_root=root,
                now=datetime(2026, 8, 17, 22, 0, tzinfo=timezone.utc),
            )
            packet = MODULE.status(project_root=PROJECT_ROOT, data_root=root)
            self.assertEqual(
                "2026-08-17T21:00:00Z",
                packet["project"]["taskCheckpoints"]["task-1"],
            )

    def test_raw_packet_detection_handles_minified_reordered_and_embedded_json(self):
        packets = (
            '{"operation":"read-visible","messages":[]}',
            '{"tasks":[],"operation" : "inventory"}',
            '```json\n{"messages" : [], "operation":"read-visible"}\n```',
            'A bad payload followed:\n{"turns":[],"operation":"read-visible"}',
            '{"reasoning":{"content":"hidden"}}',
            'Payload fragments: {"arguments":{},"result":{}}',
        )
        for packet in packets:
            report = (
                "# Daily Reflection\n\n## Scope and omissions\n\nSafe scope.\n\n"
                "## Applied local changes\n\nNone.\n\n" + packet
            )
            with self.subTest(packet=packet[:30]):
                with self.assertRaisesRegex(MODULE.ValidationError, "raw task packet"):
                    MODULE.validate_envelope(valid_payload(reportMarkdown=report))

    def test_status_is_read_only_and_project_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"
            before = MODULE.status(project_root=r"C:\Git\Example", data_root=root)
            self.assertEqual(0, before["generation"])
            self.assertIsNone(before["project"])
            self.assertFalse(root.exists())

            receipt = MODULE.commit(
                valid_payload(friction=[]),
                project_root=r"C:\Git\Example",
                data_root=root,
                now=NOW,
            )
            after = MODULE.status(project_root=r"C:\Git\Example", data_root=root)
            self.assertEqual(receipt["state"]["projectKey"], after["projectKey"])
            self.assertEqual(1, after["generation"])
            self.assertEqual(receipt["report"]["path"], after["project"]["lastReport"])

    def test_cli_input_status_and_error_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "codex-home"
            project = base / "project"
            project.mkdir()
            envelope = base / "commit.json"
            envelope.write_text(json.dumps(valid_payload()), encoding="utf-8")
            environment = dict(os.environ, CODEX_HOME=str(home))

            committed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "commit",
                    "--project-root",
                    str(project),
                    "--input",
                    str(envelope),
                ],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(0, committed.returncode, committed.stderr)
            receipt = json.loads(committed.stdout)
            self.assertTrue(receipt["state"]["advanced"])

            checked = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "status",
                    "--project-root",
                    str(project),
                ],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(0, checked.returncode, checked.stderr)
            self.assertEqual(1, json.loads(checked.stdout)["generation"])

            envelope.write_text("not-json-TOPSECRET", encoding="utf-8")
            rejected = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "commit",
                    "--project-root",
                    str(project),
                    "--input",
                    str(envelope),
                ],
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            self.assertEqual(2, rejected.returncode)
            self.assertNotIn("TOPSECRET", rejected.stderr)

            relative_environment = dict(os.environ, CODEX_HOME=".")
            relative = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "status",
                    "--project-root",
                    str(project),
                ],
                capture_output=True,
                text=True,
                env=relative_environment,
                cwd=base,
                check=False,
            )
            self.assertEqual(3, relative.returncode)

    def test_recent_friction_is_bounded_sanitized_and_project_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"
            MODULE.commit(
                valid_payload(),
                project_root=r"C:\Git\Example",
                data_root=root,
                now=NOW,
            )
            MODULE.commit(
                valid_payload(
                    friction=[
                        {
                            "category": "tool-failure",
                            "skill": "other-skill",
                            "severity": "non-blocker",
                            "summary": "A second project's tool retried.",
                            "impact": "One retry was needed.",
                            "confidence": "medium",
                            "scope": "project",
                        }
                    ]
                ),
                project_root=r"C:\Git\Other",
                data_root=root,
                now=datetime(2026, 8, 17, 21, 0, tzinfo=timezone.utc),
            )
            packet = MODULE.recent_friction(
                project_root=r"C:\Git\Example",
                since="2026-08-17T00:00:00Z",
                limit=10,
                data_root=root,
            )
            self.assertEqual("recent-friction", packet["operation"])
            self.assertEqual(1, len(packet["entries"]))
            self.assertEqual("example-skill", packet["entries"][0]["skill"])
            self.assertNotIn("projectKey", packet["entries"][0])

    def test_recent_friction_omits_tampered_secret_and_honors_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"
            receipt = MODULE.commit(
                valid_payload(),
                project_root=r"C:\Git\Example",
                data_root=root,
                now=NOW,
            )
            key = receipt["state"]["projectKey"]
            rows = []
            for index in range(3):
                rows.append(
                    {
                        "schemaVersion": 1,
                        "projectKey": key,
                        "recordedAt": f"2026-08-17T21:0{index}:00Z",
                        "reportId": f"extra-{index}",
                        "reportPath": "reflections/2026-08-17/reflection-safe.md",
                        "category": "other",
                        "skill": "example-skill",
                        "severity": "observation",
                        "summary": f"Safe recurrence {index}",
                        "impact": "Small delay",
                        "confidence": "low",
                        "scope": "skill",
                    }
                )
            rows.append(
                {
                    **rows[-1],
                    "recordedAt": "2026-08-17T21:30:00Z",
                    "summary": "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
                }
            )
            rows.append(
                {
                    **rows[-2],
                    "recordedAt": "2026-08-17T21:40:00Z",
                    "summary": {"nested": "untrusted"},
                }
            )
            with (root / "friction.jsonl").open("a", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(row) + "\n")
            packet = MODULE.recent_friction(
                project_root=r"C:\Git\Example",
                since="2026-08-17T00:00:00Z",
                limit=2,
                data_root=root,
            )
            self.assertEqual(2, len(packet["entries"]))
            self.assertTrue(packet["omissions"]["partial"])
            self.assertEqual(2, packet["omissions"]["malformedOrUnsafeEntries"])
            self.assertNotIn("QWxh", json.dumps(packet))

    def test_recent_friction_rejects_linked_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "daily-reflection"
            root.mkdir()
            original = MODULE._is_link_or_junction

            def linked_friction(path):
                return Path(path).name == "friction.jsonl" or original(path)

            with mock.patch.object(MODULE, "_is_link_or_junction", side_effect=linked_friction):
                with self.assertRaisesRegex(MODULE.StorageError, "cannot be read through a link"):
                    MODULE.recent_friction(
                        project_root=r"C:\Git\Example",
                        since="2026-08-17T00:00:00Z",
                        data_root=root,
                    )


if __name__ == "__main__":
    unittest.main()
