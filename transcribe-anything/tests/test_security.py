from __future__ import annotations

import socket

import pytest

from transcribe_anything.errors import SecurityError
from transcribe_anything.security import redact_url, sanitize_filename, validate_url


def public_dns(_hostname: str, _port: int) -> list[str]:
    return ["8.8.8.8", "2606:4700:4700::1111"]


def test_redact_url_removes_user_info_query_and_fragment() -> None:
    value = "https://alice:secret@example.com:8443/watch/item?token=very-secret#session"

    assert redact_url(value) == "https://example.com:8443/watch/item"


@pytest.mark.parametrize(
    ("untrusted", "expected"),
    [
        ("../../A dangerous video?.mp4", "A_dangerous_video_.mp4"),
        (r"C:\uploads\CON.txt", "_CON.txt"),
        ("COM1.backup.mp3", "_COM1.backup.mp3"),
        ("résumé 🎙️.wav", "resume_.wav"),
        ("...", "media"),
    ],
)
def test_sanitize_filename_is_portable_and_traversal_safe(untrusted: str, expected: str) -> None:
    assert sanitize_filename(untrusted) == expected


def test_sanitize_filename_preserves_extension_when_truncating() -> None:
    result = sanitize_filename(f"{'x' * 200}.webm", max_length=40)

    assert len(result) == 40
    assert result.endswith(".webm")


@pytest.mark.parametrize("url", ["ftp://example.com/a", "file:///etc/passwd", "data:text/plain,a"])
def test_validate_url_accepts_only_http_and_https(url: str) -> None:
    with pytest.raises(SecurityError, match="http or https"):
        validate_url(url, dns_resolver=public_dns)


def test_validate_url_rejects_credentials_without_leaking_secrets() -> None:
    url = "https://alice:password@example.com/media?token=secret#private"

    with pytest.raises(SecurityError) as captured:
        validate_url(url, dns_resolver=public_dns)

    message = str(captured.value)
    assert "credentials" in message.lower()
    assert "alice" not in message
    assert "password" not in message
    assert "token" not in message
    assert "secret" not in message
    assert "private" not in message


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/media",
        "http://player.localhost/media",
        "http://127.0.0.1/media",
        "http://10.1.2.3/media",
        "http://169.254.10.20/media",
        "http://192.0.2.1/media",
        "http://224.0.0.1/media",
        "http://0.0.0.0/media",
        "http://[::1]/media",
        "http://[fe80::1]/media",
        "http://[ff02::1]/media",
        "http://[::]/media",
    ],
)
def test_validate_url_rejects_non_public_literal_hosts(url: str) -> None:
    with pytest.raises(SecurityError, match="not allowed|non-public"):
        validate_url(url, dns_resolver=public_dns)


def test_validate_url_rejects_when_any_dns_answer_is_non_public() -> None:
    def mixed_dns(_hostname: str, _port: int) -> list[str]:
        return ["8.8.8.8", "127.0.0.1"]

    with pytest.raises(SecurityError, match="non-public"):
        validate_url("https://example.com/media", dns_resolver=mixed_dns)


def test_validate_url_accepts_getaddrinfo_records_and_uses_url_port() -> None:
    calls: list[tuple[str, int]] = []

    def resolver(hostname: str, port: int) -> list[tuple[object, ...]]:
        calls.append((hostname, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    url = "https://example.com:9443/media?signature=secret"

    assert validate_url(url, dns_resolver=resolver) == url
    assert calls == [("example.com", 9443)]


def test_dns_failure_error_redacts_url() -> None:
    def failed_dns(_hostname: str, _port: int) -> list[str]:
        raise OSError("DNS failure containing token=secret")

    with pytest.raises(SecurityError) as captured:
        validate_url("https://example.invalid/a?token=secret#fragment", dns_resolver=failed_dns)

    message = str(captured.value)
    assert message.endswith("https://example.invalid/a")
    assert "token" not in message
    assert "secret" not in message
    assert "fragment" not in message
