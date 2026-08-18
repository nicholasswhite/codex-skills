"""Media inspection and audio normalization helpers.

This module deliberately uses FFmpeg for both probing and conversion.  That
keeps the runtime dependency small and lets the application use either a
system FFmpeg or the binary bundled by ``imageio-ffmpeg``.
"""

from __future__ import annotations

import math
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from transcribe_anything.errors import MediaError

_FFMPEG_ENV: Final = "TRANSCRIBE_ANYTHING_FFMPEG"
_PROBE_TIMEOUT_SECONDS: Final = 30.0
_NORMALIZE_TIMEOUT_SECONDS: Final = 60.0 * 60.0
_DURATION_RE: Final = re.compile(
    r"Duration:\s*(?P<hours>\d+):(?P<minutes>[0-5]\d):"
    r"(?P<seconds>[0-5]\d(?:\.\d+)?)",
    re.IGNORECASE,
)
_AUDIO_STREAM_RE: Final = re.compile(
    r"^\s*Stream\s+#.*?:\s*Audio\s*:",
    re.IGNORECASE | re.MULTILINE,
)
_REFERENCE_EXTENSIONS: Final = {
    ".concat",
    ".f4m",
    ".ffconcat",
    ".ism",
    ".m3u",
    ".m3u8",
    ".mpd",
    ".pls",
    ".sdp",
    ".smil",
}
_REFERENCE_SIGNATURES: Final = (
    b"#extm3u",
    b"ffconcat version",
    b"[playlist]",
    b"v=0\r\n",
    b"v=0\n",
)


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """The media properties needed by the transcription pipeline."""

    duration_seconds: float
    has_audio: bool


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """A normalized audio chunk and its location on the source timeline."""

    index: int
    path: Path
    start_seconds: float
    duration_seconds: float


def _resolve_candidate(candidate: str | os.PathLike[str], source: str) -> str:
    """Resolve one configured executable without falling through priorities."""

    raw = os.fspath(candidate).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1].strip()
    if not raw:
        raise MediaError(f"The {source} FFmpeg setting is empty.")

    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if path.is_file():
        return str(path.resolve())

    located = shutil.which(expanded)
    if located:
        return str(Path(located).resolve())

    raise MediaError(
        f"FFmpeg configured via {source} was not found or is not an executable: "
        f"{raw}"
    )


def find_ffmpeg(
    ffmpeg: str | os.PathLike[str] | None = None,
) -> str:
    """Find an FFmpeg executable in deterministic priority order.

    An explicitly supplied executable and the environment override are treated
    as configuration: if either is present but invalid, an error is raised
    rather than silently using a different binary.
    """

    if ffmpeg is not None:
        return _resolve_candidate(ffmpeg, "explicit")

    configured = os.environ.get(_FFMPEG_ENV)
    if configured:
        return _resolve_candidate(configured, _FFMPEG_ENV)

    on_path = shutil.which("ffmpeg")
    if on_path:
        return str(Path(on_path).resolve())

    try:
        import imageio_ffmpeg  # type: ignore[import-not-found]

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        bundled = None

    if bundled:
        try:
            return _resolve_candidate(bundled, "imageio-ffmpeg")
        except MediaError:
            pass

    raise MediaError(
        "FFmpeg is required but was not found. Install FFmpeg, put it on PATH, "
        f"set {_FFMPEG_ENV}, or install imageio-ffmpeg."
    )


def _hidden_window_kwargs() -> dict[str, object]:
    """Return subprocess options that avoid opening a console on Windows."""

    if os.name != "nt":
        return {}

    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startupinfo.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
    }


def _run_ffmpeg(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg non-interactively with bounded execution time."""

    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **_hidden_window_kwargs(),
    )


def _parse_probe_output(stderr: str) -> MediaProbe:
    """Parse the stable duration and stream markers printed by FFmpeg."""

    match = _DURATION_RE.search(stderr)
    if match is None:
        raise MediaError(
            "FFmpeg could not determine the media duration. The file may be "
            "empty, corrupt, or unsupported."
        )

    hours = float(match.group("hours"))
    minutes = float(match.group("minutes"))
    seconds = float(match.group("seconds"))
    duration = hours * 3600.0 + minutes * 60.0 + seconds
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("Media duration must be a positive, finite value.")

    return MediaProbe(
        duration_seconds=duration,
        has_audio=_AUDIO_STREAM_RE.search(stderr) is not None,
    )


def _checked_file(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.exists():
        raise MediaError(f"Media file does not exist: {candidate}")
    if not candidate.is_file():
        raise MediaError(f"Media path is not a file: {candidate}")
    resolved = candidate.resolve()
    if resolved.suffix.lower() in _REFERENCE_EXTENSIONS:
        raise MediaError(
            "Playlist and manifest files are not accepted as media inputs. "
            "Provide the underlying audio or video file instead."
        )
    try:
        with resolved.open("rb") as media_file:
            header = media_file.read(16_384).lstrip().lower()
    except OSError as exc:
        raise MediaError(f"Could not inspect media file: {resolved}") from exc
    xml_manifest = header.startswith(b"<?xml") and any(
        marker in header for marker in (b"<mpd", b"<smil", b"smoothstreamingmedia")
    )
    if header.startswith(_REFERENCE_SIGNATURES) or xml_manifest:
        raise MediaError(
            "Playlist and manifest content is not accepted as a media input. "
            "Provide the underlying audio or video file instead."
        )
    return resolved


def _error_detail(stderr: str, *, limit: int = 2_000) -> str:
    detail = stderr.strip()
    if len(detail) > limit:
        detail = "..." + detail[-limit:]
    return detail or "FFmpeg did not provide error details."


def probe_media(
    path: str | os.PathLike[str],
    ffmpeg: str | os.PathLike[str] | None = None,
) -> MediaProbe:
    """Inspect a local media file using FFmpeg's ``-i`` diagnostic output."""

    media_path = _checked_file(path)
    executable = find_ffmpeg(ffmpeg)
    argv = [executable, "-hide_banner", "-nostdin", "-i", str(media_path)]

    try:
        result = _run_ffmpeg(argv, timeout=_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            f"FFmpeg timed out while probing media after "
            f"{_PROBE_TIMEOUT_SECONDS:g} seconds: {media_path}"
        ) from exc
    except OSError as exc:
        raise MediaError(f"FFmpeg could not probe media: {exc}") from exc

    # ``ffmpeg -i <file>`` normally returns a non-zero status because no output
    # was requested, so parse its diagnostics instead of checking returncode.
    try:
        return _parse_probe_output(result.stderr)
    except MediaError as exc:
        detail = _error_detail(result.stderr)
        raise MediaError(f"Could not inspect media {media_path}: {exc} {detail}") from exc


def normalize_and_chunk(
    path: str | os.PathLike[str],
    job_dir: str | os.PathLike[str],
    chunk_seconds: float = 240,
    bitrate: str = "64k",
    ffmpeg: str | os.PathLike[str] | None = None,
    *,
    output_format: str = "mp3",
) -> list[AudioChunk]:
    """Normalize a media file to mono 16 kHz MP3 or PCM WAV chunks.

    The returned start times are cumulative durations measured from the actual
    generated chunks, which avoids assuming that codec frame boundaries align
    exactly with the requested segment duration.
    """

    media_path = _checked_file(path)
    try:
        chunk_length = float(chunk_seconds)
    except (TypeError, ValueError) as exc:
        raise MediaError("Chunk duration must be a positive, finite number.") from exc
    if not math.isfinite(chunk_length) or chunk_length <= 0:
        raise MediaError("Chunk duration must be a positive, finite number.")
    if not isinstance(bitrate, str) or not bitrate.strip():
        raise MediaError("Audio bitrate must be a non-empty string such as '64k'.")
    normalized_format = str(output_format).strip().lower()
    if normalized_format not in {"mp3", "wav"}:
        raise MediaError("Audio chunk format must be 'mp3' or 'wav'.")

    executable = find_ffmpeg(ffmpeg)
    source_probe = probe_media(media_path, ffmpeg=executable)
    if not source_probe.has_audio:
        raise MediaError(f"Media has no audio stream: {media_path}")

    output_dir = Path(job_dir).expanduser()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaError(f"Could not create audio chunk directory {output_dir}: {exc}") from exc
    if not output_dir.is_dir():
        raise MediaError(f"Audio chunk destination is not a directory: {output_dir}")
    output_dir = output_dir.resolve()

    # A per-run prefix prevents an interrupted/retried job from mistaking stale
    # files for new chunks, and also permits independent jobs to share a parent.
    prefix = f"chunk_{secrets.token_hex(6)}_"
    output_template = output_dir / f"{prefix}%05d.{normalized_format}"
    argv = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(media_path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame" if normalized_format == "mp3" else "pcm_s16le",
    ]
    if normalized_format == "mp3":
        argv.extend(["-b:a", bitrate.strip()])
    argv.extend(
        [
            "-f",
            "segment",
            "-segment_time",
            format(chunk_length, ".15g"),
            "-reset_timestamps",
            "1",
            "-segment_start_number",
            "0",
            str(output_template),
        ]
    )

    try:
        result = _run_ffmpeg(argv, timeout=_NORMALIZE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise MediaError(
            "FFmpeg timed out while normalizing audio after "
            f"{_NORMALIZE_TIMEOUT_SECONDS:g} seconds."
        ) from exc
    except OSError as exc:
        raise MediaError(f"FFmpeg could not normalize audio: {exc}") from exc

    if result.returncode != 0:
        raise MediaError(
            "FFmpeg failed while normalizing audio: "
            f"{_error_detail(result.stderr)}"
        )

    generated = sorted(output_dir.glob(f"{prefix}*.{normalized_format}"))
    if not generated:
        raise MediaError("FFmpeg completed without producing any audio chunks.")

    chunks: list[AudioChunk] = []
    start = 0.0
    for index, chunk_path in enumerate(generated):
        chunk_probe = probe_media(chunk_path, ffmpeg=executable)
        if not chunk_probe.has_audio:
            raise MediaError(f"Generated chunk has no audio stream: {chunk_path}")
        chunks.append(
            AudioChunk(
                index=index,
                path=chunk_path,
                start_seconds=start,
                duration_seconds=chunk_probe.duration_seconds,
            )
        )
        start += chunk_probe.duration_seconds

    return chunks
