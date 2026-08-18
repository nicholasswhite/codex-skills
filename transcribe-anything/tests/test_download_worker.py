import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import transcribe_anything.download_worker as worker
from transcribe_anything.download_worker import (
    GuardedYoutubeDL,
    _block_external_process,
    _download_size_hook,
    _guarded_getaddrinfo,
    _require_native_http_download,
    _single_info,
    run,
)
from transcribe_anything.errors import SecurityError


def record(address: str, port: int = 443):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]


def test_connection_time_dns_guard_allows_public_address():
    guarded = _guarded_getaddrinfo(lambda *args, **kwargs: record("8.8.8.8"))
    assert guarded("example.com", 443) == record("8.8.8.8")


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1"])
def test_connection_time_dns_guard_rejects_non_public_address(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def resolver(*args, **kwargs):
        return [(family, socket.SOCK_STREAM, 6, "", (address, 443))]

    with pytest.raises(SecurityError, match="non-public"):
        _guarded_getaddrinfo(resolver)("attacker.example", 443)


def test_guarded_downloader_keeps_only_urllib_handler():
    with GuardedYoutubeDL({"quiet": True, "proxy": ""}) as downloader:
        assert set(downloader._request_director.handlers) == {"Urllib"}


def test_guarded_downloader_allows_native_https_format():
    _require_native_http_download(
        {"url": "https://media.example/audio.m4a", "protocol": "https", "ext": "m4a"},
        {},
    )


@pytest.mark.parametrize(
    "info",
    [
        {"url": "https://media.example/live.m3u8", "protocol": "m3u8_native"},
        {"url": "rtmp://media.example/live", "protocol": "rtmp"},
        {"url": "https://media.example/live", "protocol": "https", "is_live": True},
        {"url": "https://media.example/payload.m3u", "protocol": "https", "ext": "m3u"},
        {
            "url": "https://media.example/payload.ffconcat",
            "protocol": "https",
            "ext": "ffconcat",
        },
        {
            "url": "https://media.example/watch",
            "protocol": "https",
            "requested_formats": [
                {"url": "https://media.example/video.m3u8", "protocol": "m3u8_native"}
            ],
        },
    ],
)
def test_guarded_downloader_rejects_external_or_live_formats(info):
    with pytest.raises(SecurityError):
        _require_native_http_download(info, {})


def test_worker_audit_hook_rejects_child_process_events():
    with pytest.raises(SecurityError, match="external processes"):
        _block_external_process("subprocess.Popen", ())


def test_worker_audit_hook_ignores_unrelated_events():
    assert _block_external_process("open", ()) is None


def test_download_size_hook_stops_stream_over_limit():
    hook = _download_size_hook(100)
    hook({"downloaded_bytes": 100})
    with pytest.raises(RuntimeError, match="size limit"):
        hook({"downloaded_bytes": 101})


def test_download_size_hook_rejects_known_total_before_completion():
    with pytest.raises(RuntimeError, match="size limit"):
        _download_size_hook(100)({"downloaded_bytes": 1, "total_bytes": 101})


def test_run_returns_first_download_without_max_downloads(monkeypatch, tmp_path: Path):
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, *, download):
            assert url == "https://media.example/audio.mp3"
            assert download is True
            path = tmp_path / "audio.mp3"
            path.write_bytes(b"audio")
            return {"filepath": str(path), "url": url, "protocol": "https", "ext": "mp3"}

        def prepare_filename(self, info):
            return info["filepath"]

    monkeypatch.setattr(worker, "GuardedYoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(worker, "validate_url", lambda url: url)

    result = run(
        {
            "url": "https://media.example/audio.mp3",
            "download_dir": str(tmp_path),
            "max_bytes": 100,
            "socket_timeout": 1,
        }
    )

    assert result["path"] == str(tmp_path / "audio.mp3")
    assert result["size"] == 5
    assert "max_downloads" not in captured


def test_run_completes_real_ytdlp_single_file_lifecycle(monkeypatch, tmp_path: Path):
    media = b"RIFF" + (28).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(
        4, "little"
    ) + b"\x01\x00\x01\x00\x40\x1f\x00\x00\x40\x1f\x00\x00\x01\x00\x08\x00data\x00\x00\x00\x00"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(media)))
            self.end_headers()
            self.wfile.write(media)

        def log_message(self, _format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # This test targets yt-dlp's first-download return semantics. Network
        # policy is tested separately, so the loopback fixture is explicitly
        # exempted from those guards here.
        monkeypatch.setattr(worker, "validate_url", lambda url: url)
        monkeypatch.setattr(worker, "_guarded_getaddrinfo", lambda resolver: resolver)
        url = f"http://127.0.0.1:{server.server_port}/sample.wav"
        result = run(
            {
                "url": url,
                "download_dir": str(tmp_path),
                "max_bytes": 1024,
                "socket_timeout": 2,
            }
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    path = Path(result["path"])
    assert path.is_file()
    assert path.parent == tmp_path.resolve()
    assert path.read_bytes() == media


def test_playlist_result_is_rejected_instead_of_selecting_first_entry():
    with pytest.raises(SecurityError, match="Playlist links"):
        _single_info({"entries": [{"id": "first"}, {"id": "second"}]})
