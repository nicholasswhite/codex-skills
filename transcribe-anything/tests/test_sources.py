from __future__ import annotations

from pathlib import Path

import pytest

from transcribe_anything import sources
from transcribe_anything.errors import SecurityError, SourceError


def public_dns(_hostname: str, _port: int) -> list[str]:
    return ["8.8.8.8"]


def test_resolve_local_file(tmp_path: Path) -> None:
    media = tmp_path / "An audio file.wav"
    media.write_bytes(b"audio")

    result = sources.resolve_source(media, tmp_path / "unused-job", max_bytes=5)

    assert result == sources.ResolvedSource(
        path=media.resolve(),
        kind="file",
        display_name="An_audio_file.wav",
        source_reference=str(media.resolve()),
    )
    assert not (tmp_path / "unused-job").exists()


def test_resolve_local_file_validates_existence_and_kind(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="does not exist"):
        sources.resolve_source(tmp_path / "missing.wav", tmp_path / "job")

    with pytest.raises(SourceError, match="not a file"):
        sources.resolve_source(tmp_path, tmp_path / "job")


def test_resolve_local_file_enforces_size_limit(tmp_path: Path) -> None:
    media = tmp_path / "large.wav"
    media.write_bytes(b"123456")

    with pytest.raises(SourceError, match="5-byte"):
        sources.resolve_source(media, tmp_path / "job", max_bytes=5)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "x://example.com/media"])
def test_non_http_url_is_a_security_error(tmp_path: Path, url: str) -> None:
    with pytest.raises(SecurityError, match="http or https"):
        sources.resolve_source(url, tmp_path / "job", dns_resolver=public_dns)


def install_fake_worker(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, object]]) -> None:
    def fake_worker(url: str, download_dir: Path, **kwargs: object) -> Path:
        calls.append({"url": url, "download_dir": download_dir, **kwargs})
        downloaded = download_dir / "Unsafe title & episode.mp3"
        downloaded.write_bytes(b"downloaded audio")
        return downloaded

    monkeypatch.setattr(sources, "_run_download_worker", fake_worker)


def test_resolve_url_uses_bounded_yt_dlp_and_copies_to_job_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    install_fake_worker(monkeypatch, calls)
    job_dir = tmp_path / "job"
    url = "https://example.com/watch?v=123&token=secret#player"

    result = sources.resolve_source(
        url,
        job_dir,
        max_bytes=1_024,
        socket_timeout=7.5,
        dns_resolver=public_dns,
    )

    assert result.kind == "url"
    assert result.path.parent == job_dir.resolve()
    assert result.path.read_bytes() == b"downloaded audio"
    assert result.display_name == "Unsafe_title_episode.mp3"
    assert result.source_reference == "https://example.com/watch"
    assert not any(path.name.startswith(".download-") for path in job_dir.iterdir())

    assert calls[0]["url"] == url
    assert calls[0]["max_bytes"] == 1_024
    assert calls[0]["socket_timeout"] == 7.5


def test_resolve_url_does_not_overwrite_existing_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    install_fake_worker(monkeypatch, calls)
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    existing = job_dir / "Unsafe_title_episode.mp3"
    existing.write_bytes(b"keep me")

    result = sources.resolve_source(
        "https://example.com/media",
        job_dir,
        dns_resolver=public_dns,
    )

    assert existing.read_bytes() == b"keep me"
    assert result.path.name == "Unsafe_title_episode_2.mp3"


def test_download_error_does_not_leak_query_or_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(url: str, *args: object, **kwargs: object) -> Path:
        raise RuntimeError(f"provider failed for {url}")

    monkeypatch.setattr(sources, "_run_download_worker", fail)
    url = "https://example.com/media?api_key=secret#session"

    with pytest.raises(SourceError) as captured:
        sources.resolve_source(url, tmp_path / "job", dns_resolver=public_dns)

    message = str(captured.value)
    assert message.endswith("https://example.com/media")
    assert "api_key" not in message
    assert "secret" not in message
    assert "session" not in message


def test_post_download_size_check_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    install_fake_worker(monkeypatch, calls)

    with pytest.raises(SourceError, match="exceeds"):
        sources.resolve_source(
            "https://example.com/media",
            tmp_path / "job",
            max_bytes=4,
            dns_resolver=public_dns,
        )


def test_download_worker_environment_excludes_secrets_and_proxies(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("HF_TOKEN", "secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example")
    monkeypatch.setenv("SAFE_SETTING", "kept")

    environment = sources._download_worker_env()

    assert "OPENAI_API_KEY" not in environment
    assert "HF_TOKEN" not in environment
    assert "HTTPS_PROXY" not in environment
    assert environment["SAFE_SETTING"] == "kept"
