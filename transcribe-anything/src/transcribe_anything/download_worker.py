"""Isolated, connection-time guarded yt-dlp worker.

The parent sends the source URL over stdin so credentials or signed query
parameters never appear in a process command line. Every DNS answer used by
the worker is checked immediately before connection, including redirects and
extractor-discovered media hosts.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.http import HttpFD
from yt_dlp.utils import determine_protocol

from .errors import SecurityError
from .security import ensure_public_dns_results, sanitize_filename, validate_url

_original_getaddrinfo = socket.getaddrinfo
_SELF_CONTAINED_EXTENSIONS = {
    "aac",
    "aif",
    "aiff",
    "alac",
    "amr",
    "avi",
    "flac",
    "flv",
    "m4a",
    "m4v",
    "mkv",
    "mov",
    "mp3",
    "mp4",
    "mpeg",
    "mpg",
    "oga",
    "ogg",
    "ogv",
    "opus",
    "ts",
    "wav",
    "webm",
    "wma",
    "wmv",
}


def _block_external_process(event: str, _args: tuple[Any, ...]) -> None:
    """Audit hook used by the worker to forbid child-process escape hatches."""

    if event == "subprocess.Popen" or event in {"os.system", "os.posix_spawn", "os.posix_spawnp"}:
        raise SecurityError("The guarded downloader cannot start external processes.")


def _guarded_getaddrinfo(
    resolver: Callable[..., list[tuple[Any, ...]]],
) -> Callable[..., list[tuple[Any, ...]]]:
    def guarded(host: str, port: int | str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return ensure_public_dns_results(resolver(host, port, *args, **kwargs))  # type: ignore[return-value]

    return guarded


def _download_size_hook(max_bytes: int) -> Callable[[dict[str, Any]], None]:
    """Abort streaming downloads that exceed the configured byte ceiling."""

    def enforce(status: dict[str, Any]) -> None:
        downloaded = int(status.get("downloaded_bytes") or 0)
        declared = int(status.get("total_bytes") or 0)
        if max(downloaded, declared) > max_bytes:
            raise RuntimeError("The downloaded media exceeds the configured size limit.")

    return enforce


def _request_url(request: Any) -> str:
    if isinstance(request, str):
        return request
    if hasattr(request, "url"):
        return str(request.url)
    if hasattr(request, "get_full_url"):
        return str(request.get_full_url())
    raise SecurityError("The downloader attempted an unsupported media request.")


class GuardedYoutubeDL(yt_dlp.YoutubeDL):
    """Restrict yt-dlp to Python urllib and public HTTP(S) requests."""

    def build_request_director(self, handlers: Any, preferences: Any = None) -> Any:
        urllib_handlers = [handler for handler in handlers if handler.RH_KEY == "Urllib"]
        return super().build_request_director(urllib_handlers, preferences)

    def urlopen(self, req: Any) -> Any:
        parts = urlsplit(_request_url(req))
        if parts.scheme.lower() not in {"http", "https"}:
            raise SecurityError("The downloader attempted a non-HTTP media request.")
        if parts.username is not None or parts.password is not None:
            raise SecurityError("Credentials are not allowed in media request URLs.")
        return super().urlopen(req)

    def process_info(self, info_dict: dict[str, Any]) -> Any:
        # yt-dlp can delegate live HLS, RTMP, and some other protocols to
        # FFmpeg/rtmpdump. Those child processes would not inherit this
        # worker's per-connection DNS guard. Limit link intake to downloads
        # performed by yt-dlp's native HTTP downloader; FFmpeg is still used
        # later, offline, to normalize the downloaded local file.
        _require_native_http_download(info_dict, self.params)
        return super().process_info(info_dict)


def _require_native_http_download(info: dict[str, Any], params: dict[str, Any]) -> None:
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise SecurityError("Live media links are not supported by the guarded downloader.")

    requested = info.get("requested_formats")
    formats = requested if isinstance(requested, list) and requested else [info]
    for selected in formats:
        if not isinstance(selected, dict):
            raise SecurityError("The downloader selected an invalid media format.")
        protocol = determine_protocol(selected).lower()
        parts = urlsplit(str(selected.get("url") or ""))
        if protocol not in {"http", "https"} or parts.scheme.lower() not in {"http", "https"}:
            raise SecurityError("The downloader selected a non-HTTP media protocol.")
        if parts.username is not None or parts.password is not None:
            raise SecurityError("Credentials are not allowed in media request URLs.")
        downloader = get_suitable_downloader(selected, params)
        if downloader is not HttpFD:
            raise SecurityError("The selected media format requires an external downloader.")
        extension = str(selected.get("ext") or Path(parts.path).suffix.lstrip(".")).lower()
        if extension not in _SELF_CONTAINED_EXTENSIONS:
            raise SecurityError("The selected media format is not a self-contained media file.")


def _find_download(info: dict[str, Any], downloader: GuardedYoutubeDL, root: Path) -> Path:
    values: list[object] = []
    for key in ("filepath", "_filename"):
        if info.get(key):
            values.append(info[key])
    for item in info.get("requested_downloads") or []:
        if isinstance(item, dict):
            values.extend(item.get(key) for key in ("filepath", "_filename") if item.get(key))
    with suppress(Exception):
        values.append(downloader.prepare_filename(info))

    for value in values:
        try:
            candidate = Path(os.fspath(value))
        except TypeError:
            continue
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved.is_file() and not resolved.is_symlink():
            _require_self_contained_artifact(resolved)
            return resolved

    candidates = [
        path.resolve()
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() not in {".part", ".ytdl"}
    ]
    if len(candidates) != 1:
        raise RuntimeError("The downloader did not produce exactly one media file.")
    _require_self_contained_artifact(candidates[0])
    return candidates[0]


def _require_self_contained_artifact(path: Path) -> None:
    if path.suffix.lstrip(".").lower() not in _SELF_CONTAINED_EXTENSIONS:
        raise SecurityError("The downloaded artifact is not a self-contained media file.")


def _single_info(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise RuntimeError("The downloader returned no media item.")
    entries = info.get("entries")
    if entries is not None:
        raise SecurityError("Playlist links are not supported; provide one media item.")
    return info


def run(payload: dict[str, Any]) -> dict[str, Any]:
    url = str(payload["url"])
    download_dir = Path(payload["download_dir"]).resolve(strict=True)
    max_bytes = int(payload["max_bytes"])
    socket_timeout = float(payload["socket_timeout"])
    validate_url(url)

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(download_dir / "%(title).120s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "playlistend": 1,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "max_filesize": max_bytes,
        # max_filesize alone is advisory when a server omits Content-Length.
        # The hook enforces the ceiling while bytes are streaming to disk.
        "progress_hooks": [_download_size_hook(max_bytes)],
        "socket_timeout": socket_timeout,
        "overwrites": False,
        # Do not invoke Deno/Node or download remote solver components while
        # extracting a page. The audit hook below is a second enforcement layer.
        "js_runtimes": {},
        "remote_components": set(),
        # Never delegate network access to environment proxies; a proxy could
        # fetch private addresses beyond this process's DNS guard.
        "proxy": "",
    }

    socket.getaddrinfo = _guarded_getaddrinfo(_original_getaddrinfo)
    try:
        with GuardedYoutubeDL(options) as downloader:
            info = _single_info(downloader.extract_info(url, download=True))
            downloaded = _find_download(info, downloader, download_dir)
    finally:
        socket.getaddrinfo = _original_getaddrinfo

    size = downloaded.stat().st_size
    if size > max_bytes:
        raise RuntimeError("Downloaded media exceeds the configured size limit.")
    return {"path": str(downloaded), "name": sanitize_filename(downloaded.name), "size": size}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        sys.addaudithook(_block_external_process)
        result = run(payload)
    except SecurityError:
        print(json.dumps({"error": "security"}))
        return 3
    except Exception:
        print(json.dumps({"error": "download"}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
