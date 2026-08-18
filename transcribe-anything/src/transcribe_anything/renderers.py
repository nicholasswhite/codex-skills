"""Deterministic text and subtitle renderers for transcript documents."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
from contextlib import suppress
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from .schema import TranscriptDocument, TranscriptSegment

SUPPORTED_FORMATS = frozenset({"txt", "md", "json", "srt", "vtt"})
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _timestamp(seconds: float, decimal_separator: str = ".") -> str:
    """Format a subtitle timestamp without wrapping after 24 hours."""

    try:
        milliseconds = int(
            (Decimal(str(seconds)) * 1000).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    except (InvalidOperation, ValueError) as exc:  # Defensive for mutated docs.
        raise ValueError("timestamp must be a finite nonnegative number") from exc
    if milliseconds < 0:
        raise ValueError("timestamp must be nonnegative")
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1_000)
    return (
        f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"
        f"{decimal_separator}{millis:03d}"
    )


def _cue_text(segment: TranscriptSegment) -> str:
    if segment.speaker:
        return f"{segment.speaker}: {segment.text}"
    return segment.text


def _render_txt(document: TranscriptDocument) -> str:
    if not document.segments:
        return f"{document.text}\n" if document.text else ""

    lines: list[str] = []
    for segment in document.segments:
        time_range = (
            f"{_timestamp(segment.start_seconds)} --> "
            f"{_timestamp(segment.end_seconds)}"
        )
        cue = _cue_text(segment).replace("\n", "\n    ")
        lines.append(f"[{time_range}] {cue}")
    return "\n".join(lines) + "\n"


def _escape_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    return re.sub(r"([`*_{}\[\]<>])", r"\\\1", escaped)


def _render_md(document: TranscriptDocument) -> str:
    lines = ["# Transcript", ""]
    lines.extend(
        [
            f"- Provider: {_escape_markdown(document.provider)}",
            f"- Model: {_escape_markdown(document.model)}",
            f"- Duration: {_timestamp(document.duration_seconds)}",
        ]
    )
    if document.language:
        lines.append(f"- Language: {_escape_markdown(document.language)}")
    lines.extend(
        [
            f"- Created: {_escape_markdown(document.created_at)}",
            "- Source: `"
            + json.dumps(
                document.source,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).replace("`", "\\u0060")
            + "`",
            "",
            "## Transcript",
            "",
        ]
    )

    if document.segments:
        for segment in document.segments:
            lines.append(
                "### "
                f"{_timestamp(segment.start_seconds)} → "
                f"{_timestamp(segment.end_seconds)}"
            )
            lines.append("")
            if segment.speaker:
                lines.append(f"**{_escape_markdown(segment.speaker)}:** {segment.text}")
            else:
                lines.append(segment.text)
            lines.append("")
    elif document.text:
        lines.extend([document.text, ""])
    else:
        lines.extend(["_No transcript text available._", ""])

    if document.warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {_escape_markdown(warning)}" for warning in document.warnings)
        lines.append("")
    return "\n".join(lines)


def _render_json(document: TranscriptDocument) -> str:
    return (
        json.dumps(
            document.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _render_subtitles(document: TranscriptDocument, *, webvtt: bool) -> str:
    if not document.segments:
        return "WEBVTT\n\n" if webvtt else ""

    separator = "." if webvtt else ","
    blocks: list[str] = []
    if webvtt:
        blocks.extend(["WEBVTT", ""])
    for cue_number, segment in enumerate(document.segments, start=1):
        blocks.extend(
            [
                str(cue_number),
                f"{_timestamp(segment.start_seconds, separator)} --> "
                f"{_timestamp(segment.end_seconds, separator)}",
                _cue_text(segment),
                "",
            ]
        )
    return "\n".join(blocks)


def _normalise_format(format_name: str) -> str:
    if not isinstance(format_name, str):
        raise ValueError(
            "format must be one of: " + ", ".join(sorted(SUPPORTED_FORMATS))
        )
    normalised = format_name.strip().lower()
    if normalised not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unsupported transcript format {format_name!r}; expected one of: "
            + ", ".join(sorted(SUPPORTED_FORMATS))
        )
    return normalised


def render_document(document: TranscriptDocument, format: str) -> str:
    """Render ``document`` in one of the supported output formats."""

    if not isinstance(document, TranscriptDocument):
        raise TypeError("document must be a TranscriptDocument")
    format_name = _normalise_format(format)
    if format_name == "txt":
        return _render_txt(document)
    if format_name == "md":
        return _render_md(document)
    if format_name == "json":
        return _render_json(document)
    if format_name == "srt":
        return _render_subtitles(document, webvtt=False)
    return _render_subtitles(document, webvtt=True)


def _sanitise_basename(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("basename must be a string")
    # Split both path separator styles so this remains safe on every platform.
    leaf = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
    leaf = unicodedata.normalize("NFKC", leaf)
    safe = "".join(
        character if (character.isalnum() or character in "._-") else "_"
        for character in leaf
    )
    safe = re.sub(r"_+", "_", safe).strip(" ._-")
    if not safe:
        safe = "transcript"
    if safe.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        safe = f"_{safe}"
    # Keep paths practical while retaining a useful identifying prefix.
    safe = safe[:128].rstrip(" .") or "transcript"
    return safe


def write_outputs(
    document: TranscriptDocument,
    output_dir: str | Path,
    basename: str,
    formats: list[str] | tuple[str, ...],
) -> dict[str, Path]:
    """Render and atomically-independent write a set of transcript outputs."""

    if not isinstance(document, TranscriptDocument):
        raise TypeError("document must be a TranscriptDocument")
    if isinstance(formats, (str, bytes)):
        raise TypeError("formats must be an iterable of format names")
    try:
        format_names = [_normalise_format(item) for item in formats]
    except TypeError as exc:
        raise TypeError("formats must be an iterable of format names") from exc

    # De-duplicate without changing the caller's requested order.
    format_names = list(dict.fromkeys(format_names))
    safe_basename = _sanitise_basename(basename)
    rendered = {
        format_name: render_document(document, format_name)
        for format_name in format_names
    }

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    targets = {
        format_name: destination / f"{safe_basename}.{format_name}"
        for format_name in format_names
    }
    invalid = [path for path in targets.values() if path.exists() and not path.is_file()]
    if invalid:
        raise OSError(f"Transcript output target is not a regular file: {invalid[0]}")

    staging = Path(tempfile.mkdtemp(prefix=".transcribe-stage-", dir=destination))
    staged_files: dict[str, Path] = {}
    backups: dict[Path, Path] = {}
    promoted: list[Path] = []
    preserve_staging = False
    try:
        for format_name, content in rendered.items():
            staged = staging / f"new-{format_name}"
            with staged.open("w", encoding="utf-8", newline="\n") as output_file:
                output_file.write(content)
            staged_files[format_name] = staged

        backup_dir = staging / "backups"
        backup_dir.mkdir()
        for format_name, target in targets.items():
            if target.exists():
                backup = backup_dir / format_name
                os.replace(target, backup)
                backups[target] = backup

        for format_name, target in targets.items():
            os.replace(staged_files[format_name], target)
            promoted.append(target)
    except BaseException as original:
        for target in reversed(promoted):
            with suppress(OSError):
                target.unlink(missing_ok=True)
        restore_errors: list[OSError] = []
        for target, backup in backups.items():
            if backup.exists():
                try:
                    os.replace(backup, target)
                except OSError as exc:
                    restore_errors.append(exc)
        if restore_errors:
            preserve_staging = True
            raise OSError(
                "Transcript output rollback was incomplete. Previous outputs are "
                f"preserved for manual recovery in: {staging}"
            ) from original
        raise
    finally:
        if not preserve_staging:
            shutil.rmtree(staging, ignore_errors=True)

    return targets
