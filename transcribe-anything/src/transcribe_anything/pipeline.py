"""End-to-end orchestration from a path or URL to transcript artifacts."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError, OutputError
from .media import normalize_and_chunk, probe_media
from .provider import AudioCppProvider, OpenAIProvider, TranscriptionProvider
from .renderers import SUPPORTED_FORMATS, write_outputs
from .schema import TranscriptDocument
from .settings import DEFAULT_MODELS, MAX_CHUNK_SECONDS, SUPPORTED_PROVIDERS, Settings
from .sources import resolve_source

PipelineProgress = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    document: TranscriptDocument
    files: dict[str, Path]
    output_dir: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as media_file:
        for block in iter(lambda: media_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalise_formats(formats: Iterable[str]) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(str(item).strip().lower() for item in formats))
    if not values:
        raise OutputError("Select at least one transcript output format.")
    unsupported = sorted(set(values) - SUPPORTED_FORMATS)
    if unsupported:
        raise OutputError(
            "Unsupported transcript format(s): "
            + ", ".join(unsupported)
            + ". Expected: "
            + ", ".join(sorted(SUPPORTED_FORMATS))
        )
    return values


def _output_basename(
    display_name: str,
    destination: Path,
    formats: Iterable[str],
    source_path: Path,
) -> str:
    basename = Path(display_name).stem or "transcript"
    source_key = os.path.normcase(str(source_path.resolve()))
    for format_name in formats:
        candidate_key = os.path.normcase(
            str((destination / f"{basename}.{format_name}").resolve())
        )
        if candidate_key == source_key:
            return f"{basename}-transcript"
    return basename


def transcribe(
    source: str | Path,
    *,
    output_dir: str | Path,
    formats: Iterable[str] = ("txt", "md", "json", "srt", "vtt"),
    language: str | None = None,
    model: str | None = None,
    provider_name: str | None = None,
    audiocpp_base_url: str | None = None,
    settings: Settings | None = None,
    provider: TranscriptionProvider | None = None,
    progress: PipelineProgress | None = None,
) -> TranscriptionResult:
    """Resolve, normalize, transcribe, and render one media source.

    Temporary downloads and normalized chunks live inside a per-job temporary
    directory and are deleted whether the job succeeds or fails. Only rendered
    transcript artifacts are retained in ``output_dir``.
    """

    resolved_settings = settings or Settings.from_env()
    requested_formats = _normalise_formats(formats)
    if provider is not None:
        active_provider = provider
    else:
        selected_provider_name = (provider_name or resolved_settings.provider).strip().lower()
        if selected_provider_name not in SUPPORTED_PROVIDERS:
            expected = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ConfigurationError(
                f"Transcription provider must be one of: {expected}."
            )
        default_model = (
            resolved_settings.model
            if selected_provider_name == resolved_settings.provider
            else DEFAULT_MODELS[selected_provider_name]
        )
        selected_model = (model or default_model).strip()
        if not selected_model:
            raise ConfigurationError("A transcription model is required.")
        if selected_provider_name == "audiocpp":
            active_provider = AudioCppProvider(
                model=selected_model,
                base_url=audiocpp_base_url or resolved_settings.audiocpp_base_url,
            )
        else:
            active_provider = OpenAIProvider(model=selected_model)
    destination = Path(output_dir).expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="transcribe-anything-") as temporary:
        job_dir = Path(temporary)
        if progress:
            progress("resolving", 0, 1)
        resolved = resolve_source(
            source,
            job_dir / "source",
            max_bytes=resolved_settings.max_source_bytes,
        )
        if progress:
            progress("resolving", 1, 1)

        if progress:
            progress("inspecting", 0, 1)
        probe = probe_media(resolved.path, ffmpeg=resolved_settings.ffmpeg)
        if progress:
            progress("inspecting", 1, 1)

        if progress:
            progress("normalizing", 0, 1)
        chunks = normalize_and_chunk(
            resolved.path,
            job_dir / "audio",
            chunk_seconds=min(resolved_settings.chunk_seconds, MAX_CHUNK_SECONDS),
            output_format=getattr(active_provider, "audio_format", "mp3"),
            ffmpeg=resolved_settings.ffmpeg,
        )
        if progress:
            progress("normalizing", 1, 1)

        provider_result = active_provider.transcribe(
            chunks,
            language=language,
            progress=progress,
        )
        source_metadata = {
            "kind": resolved.kind,
            "name": resolved.display_name,
            "reference": resolved.source_reference,
            "sha256": _sha256(resolved.path),
        }
        document = TranscriptDocument(
            source=source_metadata,
            provider=active_provider.name,
            model=active_provider.model,
            duration_seconds=probe.duration_seconds,
            text=provider_result.text,
            segments=provider_result.segments,
            language=provider_result.language,
            warnings=provider_result.warnings,
        )

        if progress:
            progress("writing", 0, len(requested_formats))
        try:
            basename = _output_basename(
                resolved.display_name,
                destination,
                requested_formats,
                resolved.path,
            )
            files = write_outputs(
                document,
                destination,
                basename,
                requested_formats,
            )
        except (OSError, TypeError, ValueError) as exc:
            raise OutputError(f"Could not write transcript outputs: {exc}") from exc
        if progress:
            progress("writing", len(files), len(requested_formats))

    return TranscriptionResult(document=document, files=files, output_dir=destination)
