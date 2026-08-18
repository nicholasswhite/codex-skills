from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transcribe_anything.renderers import render_document, write_outputs
from transcribe_anything.schema import TranscriptDocument, TranscriptSegment


def _document() -> TranscriptDocument:
    return TranscriptDocument(
        source={"kind": "url", "url": "https://example.test/watch?a=1&b=2"},
        provider="test-provider",
        model="test-model",
        duration_seconds=90_062.345,
        text="This aggregate must not be duplicated.",
        segments=[
            TranscriptSegment(90_061.2, 90_062.345, "Hello, café & <world>!", "Dr. A&B"),
            TranscriptSegment(90_062.345, 90_062.345, "Second line"),
            TranscriptSegment(3, 4, "  "),
        ],
        language="en",
        created_at="2026-08-13T12:00:00Z",
    )


class RendererTests(unittest.TestCase):
    def test_plain_text_uses_segments_once_and_includes_speaker(self) -> None:
        rendered = render_document(_document(), "txt")
        self.assertIn("Dr. A&B: Hello, café & <world>!", rendered)
        self.assertIn("Second line", rendered)
        self.assertNotIn("aggregate", rendered)

    def test_json_is_unicode_deterministic_and_valid(self) -> None:
        document = _document()
        first = render_document(document, "json")
        second = render_document(document, "JSON")
        self.assertEqual(first, second)
        self.assertIn("café", first)
        self.assertNotIn("caf\\u00e9", first)
        self.assertEqual(json.loads(first), document.to_dict())

    def test_srt_and_vtt_number_cues_and_support_more_than_24_hours(self) -> None:
        srt = render_document(_document(), "srt")
        self.assertTrue(srt.startswith("1\n25:01:01,200 --> 25:01:02,345\n"))
        self.assertIn("Dr. A&B: Hello, café & <world>!", srt)
        self.assertIn("\n2\n25:01:02,345 --> 25:01:02,345\nSecond line\n", srt)

        vtt = render_document(_document(), "vtt")
        self.assertTrue(vtt.startswith("WEBVTT\n\n1\n25:01:01.200"))
        self.assertIn("\n2\n25:01:02.345 --> 25:01:02.345\n", vtt)

    def test_markdown_preserves_transcript_special_characters(self) -> None:
        markdown = render_document(_document(), "md")
        self.assertIn("Hello, café & <world>!", markdown)
        self.assertIn("**Dr. A&B:**", markdown)

    def test_empty_segments_fall_back_to_document_text(self) -> None:
        document = TranscriptDocument(
            source={},
            provider="p",
            model="m",
            duration_seconds=0,
            text="Only aggregate text",
            segments=[TranscriptSegment(0, 0, "  ")],
            created_at="2026-08-13T00:00:00Z",
        )
        self.assertEqual(render_document(document, "txt"), "Only aggregate text\n")
        self.assertEqual(render_document(document, "srt"), "")
        self.assertEqual(render_document(document, "vtt"), "WEBVTT\n\n")

    def test_invalid_format_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported transcript format"):
            render_document(_document(), "docx")

    def test_write_outputs_sanitises_basename_and_writes_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            outputs = write_outputs(
                _document(),
                temporary_directory,
                "../A bad:name",
                ["TXT", "json", "txt"],
            )
            self.assertEqual(list(outputs), ["txt", "json"])
            self.assertEqual(outputs["txt"].name, "A_bad_name.txt")
            self.assertEqual(outputs["json"].name, "A_bad_name.json")
            self.assertEqual(
                Path(outputs["txt"]).read_text(encoding="utf-8"),
                render_document(_document(), "txt"),
            )
            self.assertIn("café", outputs["json"].read_text(encoding="utf-8"))

    def test_write_outputs_does_not_leave_partial_files_on_invalid_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            (destination / "recording.json").mkdir()

            with self.assertRaisesRegex(OSError, "not a regular file"):
                write_outputs(
                    _document(),
                    destination,
                    "recording",
                    ("txt", "json"),
                )

            self.assertFalse((destination / "recording.txt").exists())

    def test_write_outputs_rolls_back_when_promotion_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            text_target = destination / "recording.txt"
            json_target = destination / "recording.json"
            text_target.write_text("old text", encoding="utf-8")
            json_target.write_text("old json", encoding="utf-8")
            real_replace = os.replace
            failed = False

            def fail_second_promotion(source: str | Path, target: str | Path) -> None:
                nonlocal failed
                if Path(source).name == "new-json" and not failed:
                    failed = True
                    raise OSError("injected promotion failure")
                real_replace(source, target)

            with patch(
                "transcribe_anything.renderers.os.replace",
                side_effect=fail_second_promotion,
            ), self.assertRaisesRegex(OSError, "injected promotion failure"):
                write_outputs(
                    _document(),
                    destination,
                    "recording",
                    ("txt", "json"),
                )

            self.assertEqual(text_target.read_text(encoding="utf-8"), "old text")
            self.assertEqual(json_target.read_text(encoding="utf-8"), "old json")

    def test_write_outputs_preserves_backups_when_restore_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory)
            text_target = destination / "recording.txt"
            json_target = destination / "recording.json"
            text_target.write_text("old text", encoding="utf-8")
            json_target.write_text("old json", encoding="utf-8")
            real_replace = os.replace

            def fail_promotion_and_one_restore(source: str | Path, target: str | Path) -> None:
                source_path = Path(source)
                target_path = Path(target)
                if source_path.name == "new-json":
                    raise OSError("injected promotion failure")
                if source_path.name == "txt" and target_path == text_target:
                    raise OSError("injected restore failure")
                real_replace(source, target)

            with patch(
                "transcribe_anything.renderers.os.replace",
                side_effect=fail_promotion_and_one_restore,
            ), self.assertRaisesRegex(OSError, "preserved for manual recovery"):
                write_outputs(_document(), destination, "recording", ("txt", "json"))

            self.assertEqual(json_target.read_text(encoding="utf-8"), "old json")
            stages = list(destination.glob(".transcribe-stage-*"))
            self.assertEqual(len(stages), 1)
            self.assertEqual((stages[0] / "backups" / "txt").read_text(), "old text")


if __name__ == "__main__":
    unittest.main()
