from __future__ import annotations

import unittest

from transcribe_anything.schema import TranscriptDocument, TranscriptSegment


class TranscriptSchemaTests(unittest.TestCase):
    def test_document_normalises_segments_and_strips_empty_ones(self) -> None:
        document = TranscriptDocument(
            source={"kind": "file", "name": "sample.wav"},
            provider=" local ",
            model=" test-model ",
            duration_seconds=-2,
            text="",
            segments=[
                TranscriptSegment(4, 5, " later ", " Bob "),
                TranscriptSegment(-1, 1.5, " first "),
                TranscriptSegment(2, 1, "   "),
                TranscriptSegment(1, 3, " second ", ""),
            ],
            language=" en ",
            warnings=[" low confidence ", ""],
            created_at="2026-08-13T12:00:00-04:00",
        )

        self.assertEqual(
            [segment.text for segment in document.segments],
            ["first", "second", "later"],
        )
        self.assertEqual(
            [
                (segment.start_seconds, segment.end_seconds)
                for segment in document.segments
            ],
            [(0.0, 1.5), (1.5, 3.0), (4.0, 5.0)],
        )
        self.assertEqual(document.duration_seconds, 5.0)
        self.assertEqual(document.text, "first\nsecond\nlater")
        self.assertEqual(document.provider, "local")
        self.assertEqual(document.model, "test-model")
        self.assertEqual(document.language, "en")
        self.assertEqual(document.warnings, ["low confidence"])
        self.assertEqual(document.created_at, "2026-08-13T16:00:00Z")

    def test_to_dict_is_complete_and_detached(self) -> None:
        source = {"kind": "url", "metadata": {"title": "A & B"}}
        document = TranscriptDocument(
            source=source,
            provider="provider",
            model="model",
            duration_seconds=1,
            text="café",
            segments=[TranscriptSegment(0, 1, "café", "Speaker <1>")],
            created_at="2026-08-13T00:00:00Z",
        )

        payload = document.to_dict()
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["language"], None)
        self.assertEqual(
            payload["segments"],
            [
                {
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "text": "café",
                    "speaker": "Speaker <1>",
                }
            ],
        )
        source["metadata"]["title"] = "changed"
        self.assertEqual(document.source["metadata"]["title"], "A & B")
        payload["source"]["metadata"]["title"] = "also changed"  # type: ignore[index]
        self.assertEqual(document.source["metadata"]["title"], "A & B")

    def test_invalid_numeric_and_collection_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TranscriptSegment(0, float("nan"), "bad")
        with self.assertRaises(TypeError):
            TranscriptDocument(
                source=[],  # type: ignore[arg-type]
                provider="p",
                model="m",
                duration_seconds=0,
                text="",
            )
        with self.assertRaises(TypeError):
            TranscriptDocument(
                source={},
                provider="p",
                model="m",
                duration_seconds=0,
                text="",
                warnings="not a list",  # type: ignore[arg-type]
            )


if __name__ == "__main__":
    unittest.main()
