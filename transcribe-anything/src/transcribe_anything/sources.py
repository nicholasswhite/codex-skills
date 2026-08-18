"""Resolve local files and public media URLs into transcription inputs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from .errors import SourceError
from .security import DNSResolver, redact_url, sanitize_filename, validate_url

DEFAULT_MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_SOCKET_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    """A validated local path and non-sensitive description of its origin."""

    path: Path
    kind: Literal["file", "url"]
    display_name: str
    source_reference: str


def _validate_limits(max_bytes: int, socket_timeout: float) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if (
        isinstance(socket_timeout, bool)
        or not isinstance(socket_timeout, (int, float))
        or socket_timeout <= 0
    ):
        raise ValueError("socket_timeout must be positive")


def _prepare_job_dir(job_dir: str | os.PathLike[str]) -> Path:
    directory = Path(job_dir).expanduser()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise SourceError(f"Could not create job directory: {directory}") from None
    if not directory.is_dir():
        raise SourceError(f"Job directory is not a directory: {directory}")
    try:
        return directory.resolve(strict=True)
    except OSError:
        raise SourceError(f"Could not access job directory: {directory}") from None


def _resolve_local(source: str | os.PathLike[str], *, max_bytes: int) -> ResolvedSource:
    path = Path(source).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SourceError(f"Local media file does not exist: {path}") from None
    if not resolved.is_file():
        raise SourceError(f"Local media source is not a file: {resolved}")
    try:
        size = resolved.stat().st_size
    except OSError:
        raise SourceError(f"Could not inspect local media file: {resolved}") from None
    if size > max_bytes:
        raise SourceError(
            f"Local media file exceeds the {max_bytes}-byte size limit: {resolved.name}"
        )

    return ResolvedSource(
        path=resolved,
        kind="file",
        display_name=sanitize_filename(resolved.name),
        source_reference=str(resolved),
    )


def _safe_download_candidate(value: object, download_dir: Path) -> Path | None:
    try:
        candidate = Path(os.fspath(value))
    except TypeError:
        return None
    if not candidate.is_absolute():
        candidate = download_dir / candidate
    try:
        candidate = candidate.resolve(strict=True)
        candidate.relative_to(download_dir)
    except (OSError, RuntimeError, ValueError):
        return None
    if not candidate.is_file() or candidate.is_symlink():
        return None
    return candidate


def _hidden_window_kwargs() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startupinfo.wShowWindow = subprocess.SW_HIDE  # type: ignore[attr-defined]
    return {
        "startupinfo": startupinfo,
        "creationflags": subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
    }


def _download_worker_env() -> dict[str, str]:
    sensitive = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    blocked_names = {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in blocked_names
        and not any(fragment in name.upper() for fragment in sensitive)
    }


def _run_download_worker(
    url: str,
    download_dir: Path,
    *,
    max_bytes: int,
    socket_timeout: float,
) -> Path:
    payload = json.dumps(
        {
            "url": url,
            "download_dir": str(download_dir),
            "max_bytes": max_bytes,
            "socket_timeout": socket_timeout,
        }
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "transcribe_anything.download_worker"],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2 * 60 * 60,
            check=False,
            env=_download_worker_env(),
            **_hidden_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError("The isolated media downloader could not complete.") from exc

    output_lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        manifest = json.loads(output_lines[-1]) if output_lines else {}
    except json.JSONDecodeError:
        manifest = {}
    if result.returncode == 3 or manifest.get("error") == "security":
        raise SourceError("The media link attempted to access a non-public network address.")
    if result.returncode != 0:
        raise SourceError("The media provider could not download this public link.")
    candidate = _safe_download_candidate(manifest.get("path"), download_dir)
    if candidate is None:
        raise SourceError("The media provider produced an invalid download artifact.")
    return candidate


def _unique_destination(job_dir: Path, filename: str) -> Path:
    candidate = job_dir / filename
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    path = Path(filename)
    for number in range(2, 10_000):
        candidate = job_dir / f"{path.stem}_{number}{path.suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise SourceError("Could not allocate a unique media filename in the job directory.")


def _download_url(
    source: str,
    job_dir: Path,
    *,
    max_bytes: int,
    socket_timeout: float,
    dns_resolver: DNSResolver | None,
) -> ResolvedSource:
    validated_url = validate_url(source, dns_resolver=dns_resolver)
    source_reference = redact_url(validated_url)
    try:
        with tempfile.TemporaryDirectory(prefix=".download-", dir=job_dir) as temporary:
            download_dir = Path(temporary).resolve(strict=True)
            downloaded = _run_download_worker(
                validated_url,
                download_dir,
                max_bytes=max_bytes,
                socket_timeout=socket_timeout,
            )

            try:
                downloaded_size = downloaded.stat().st_size
            except OSError:
                raise SourceError("Could not inspect the downloaded media file.") from None
            if downloaded_size > max_bytes:
                raise SourceError(f"Downloaded media exceeds the {max_bytes}-byte size limit.")

            safe_name = sanitize_filename(downloaded.name)
            destination = _unique_destination(job_dir, safe_name)
            shutil.copy2(downloaded, destination)
    except SourceError:
        raise
    except Exception:
        # Do not reflect downloader exception text: providers often include the
        # complete (potentially credential-bearing) URL in those messages.
        raise SourceError(f"Could not download media from {source_reference}") from None

    return ResolvedSource(
        path=destination.resolve(strict=True),
        kind="url",
        display_name=destination.name,
        source_reference=source_reference,
    )


def _is_url_input(source: str | os.PathLike[str]) -> bool:
    if not isinstance(source, str):
        return False
    # A drive-qualified Windows path is local even though urlsplit sees its
    # drive letter as a URI scheme.
    if re.match(r"^[A-Za-z]:(?!//)", source):
        return False
    try:
        return bool(urlsplit(source).scheme)
    except ValueError:
        # Malformed strings that look URL-like should receive a security error,
        # not be treated as arbitrary local paths.
        return "://" in source


def resolve_source(
    source: str | os.PathLike[str],
    job_dir: str | os.PathLike[str],
    *,
    max_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    socket_timeout: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
    dns_resolver: DNSResolver | None = None,
) -> ResolvedSource:
    """Resolve one local path or HTTP(S) URL for the transcription pipeline."""

    _validate_limits(max_bytes, socket_timeout)
    if _is_url_input(source):
        directory = _prepare_job_dir(job_dir)
        return _download_url(
            os.fspath(source),
            directory,
            max_bytes=max_bytes,
            socket_timeout=socket_timeout,
            dns_resolver=dns_resolver,
        )
    return _resolve_local(source, max_bytes=max_bytes)


__all__ = [
    "DEFAULT_MAX_SOURCE_BYTES",
    "DEFAULT_SOCKET_TIMEOUT_SECONDS",
    "ResolvedSource",
    "resolve_source",
]
