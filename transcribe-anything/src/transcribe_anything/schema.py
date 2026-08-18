"""Canonical transcript data structures.

The schema deliberately has no provider-specific fields.  Provider adapters can
put identifying information in ``source`` while renderers and downstream tools
can rely on a small, stable JSON-compatible representation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar


def _utc_now() -> str:
    """Return a compact, unambiguous UTC timestamp."""

    return datetime.now(UTC).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _normalise_created_at(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("created_at must be a non-empty ISO-8601 string")

    candidate = value.strip()
    parseable = candidate[:-1] + "+00:00" if candidate.endswith(("Z", "z")) else candidate
    try:
        parsed = datetime.fromisoformat(parseable)
    except ValueError as exc:
        raise ValueError("created_at must be a valid ISO-8601 timestamp") from exc

    # Treat a timezone-less timestamp as UTC.  This keeps hand-authored and
    # legacy documents usable while guaranteeing that serialised values are
    # explicit about their timezone.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    return parsed.isoformat().replace("+00:00", "Z")


def _normalise_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a finite number")
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    # Small negative values occasionally occur after timestamp conversion.
    # Clamping all negative values gives every document the same nonnegative
    # invariant and avoids emitting invalid subtitle timestamps.
    return max(0.0, result)


def _clean_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


@dataclass(slots=True)
class TranscriptSegment:
    """A single, normalised portion of a transcript."""

    start_seconds: float
    end_seconds: float
    text: str
    speaker: str | None = None

    def __post_init__(self) -> None:
        self.start_seconds = _normalise_seconds(
            self.start_seconds, "start_seconds"
        )
        self.end_seconds = max(
            self.start_seconds,
            _normalise_seconds(self.end_seconds, "end_seconds"),
        )
        self.text = _clean_text(self.text, "text")
        if self.speaker is not None:
            self.speaker = _clean_text(self.speaker, "speaker") or None

    def to_dict(self) -> dict[str, object]:
        return {
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
            "speaker": self.speaker,
        }


@dataclass(slots=True)
class TranscriptDocument:
    """Provider-neutral transcript and its provenance."""

    schema_version: ClassVar[str] = "1.0"

    source: Mapping[str, Any]
    provider: str
    model: str
    duration_seconds: float
    text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    language: str | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.source, Mapping):
            raise TypeError("source must be a mapping")
        self.source = deepcopy(dict(self.source))
        self.provider = _clean_text(self.provider, "provider")
        self.model = _clean_text(self.model, "model")
        self.text = _clean_text(self.text, "text")
        self.duration_seconds = _normalise_seconds(
            self.duration_seconds, "duration_seconds"
        )

        if self.language is not None:
            self.language = _clean_text(self.language, "language") or None

        if self.segments is None:  # type: ignore[comparison-overlap]
            raw_segments: list[TranscriptSegment | Mapping[str, Any]] = []
        elif isinstance(self.segments, (str, bytes)):
            raise TypeError("segments must be an iterable of transcript segments")
        else:
            try:
                raw_segments = list(self.segments)
            except TypeError as exc:
                raise TypeError(
                    "segments must be an iterable of transcript segments"
                ) from exc

        prepared: list[tuple[int, TranscriptSegment]] = []
        for index, segment in enumerate(raw_segments):
            if isinstance(segment, TranscriptSegment):
                normalised = TranscriptSegment(
                    start_seconds=segment.start_seconds,
                    end_seconds=segment.end_seconds,
                    text=segment.text,
                    speaker=segment.speaker,
                )
            elif isinstance(segment, Mapping):
                try:
                    normalised = TranscriptSegment(**dict(segment))
                except TypeError as exc:
                    raise TypeError(f"invalid transcript segment at index {index}") from exc
            else:
                raise TypeError(f"invalid transcript segment at index {index}")
            if normalised.text:
                prepared.append((index, normalised))

        # Provider responses are not uniformly ordered.  Stable chronological
        # sorting followed by clamping produces non-overlapping monotonic cues.
        prepared.sort(
            key=lambda item: (
                item[1].start_seconds,
                item[1].end_seconds,
                item[0],
            )
        )
        cursor = 0.0
        normalised_segments: list[TranscriptSegment] = []
        for _, segment in prepared:
            start = max(cursor, segment.start_seconds)
            end = max(start, segment.end_seconds)
            normalised_segments.append(
                TranscriptSegment(start, end, segment.text, segment.speaker)
            )
            cursor = end
        self.segments = normalised_segments
        self.duration_seconds = max(self.duration_seconds, cursor)

        if not self.text and self.segments:
            self.text = "\n".join(segment.text for segment in self.segments)

        if self.warnings is None:  # type: ignore[comparison-overlap]
            raw_warnings: list[str] = []
        elif isinstance(self.warnings, (str, bytes)):
            raise TypeError("warnings must be an iterable of strings")
        else:
            try:
                raw_warnings = list(self.warnings)
            except TypeError as exc:
                raise TypeError("warnings must be an iterable of strings") from exc
        self.warnings = [
            cleaned
            for warning in raw_warnings
            if (cleaned := _clean_text(warning, "warning"))
        ]
        self.created_at = _normalise_created_at(self.created_at)

    def to_dict(self) -> dict[str, object]:
        """Return a detached, JSON-compatible representation."""

        return {
            "schema_version": self.schema_version,
            "source": deepcopy(dict(self.source)),
            "provider": self.provider,
            "model": self.model,
            "duration_seconds": self.duration_seconds,
            "text": self.text,
            "segments": [segment.to_dict() for segment in self.segments],
            "language": self.language,
            "warnings": list(self.warnings),
            "created_at": self.created_at,
        }
