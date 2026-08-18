"""Security helpers for untrusted media source names and URLs.

URL validation performs an initial DNS check. The isolated download worker also
validates DNS answers at connection time so redirects, extractor-discovered
media URLs, and DNS rebinding cannot reach local networks.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from collections.abc import Callable, Iterable
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .errors import SecurityError

type DNSResult = str | bytes | ipaddress.IPv4Address | ipaddress.IPv6Address | tuple[object, ...]
type DNSResolver = Callable[[str, int], Iterable[DNSResult]]

_WINDOWS_RESERVED_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_REPEATED_UNDERSCORES = re.compile(r"_+")


def redact_url(value: str) -> str:
    """Return a URL safe for metadata and user-facing error messages.

    Query parameters and fragments commonly contain access tokens.  User info
    is removed as well, even though credential-bearing URLs are rejected.
    """

    try:
        parts = urlsplit(value)
        hostname = parts.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        netloc = hostname + (f":{port}" if port is not None else "")
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, "", ""))
    except ValueError:
        # urlsplit rejects malformed bracketed IPv6 addresses.  Keep even this
        # fallback from reflecting the two most common secret-bearing fields.
        without_secrets = value.split("#", 1)[0].split("?", 1)[0]
        return re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1", without_secrets)


def sanitize_filename(name: str, *, fallback: str = "media", max_length: int = 180) -> str:
    """Make an untrusted filename a portable, traversal-safe basename."""

    if max_length < 1:
        raise ValueError("max_length must be positive")

    # Treat both slash styles as separators regardless of the host OS.
    basename = re.split(r"[/\\]", str(name))[-1]
    normalized = unicodedata.normalize("NFKD", basename).encode("ascii", "ignore").decode("ascii")
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", normalized)
    cleaned = _REPEATED_UNDERSCORES.sub("_", cleaned).strip(" ._-")

    if not cleaned:
        normalized_fallback = (
            unicodedata.normalize("NFKD", fallback).encode("ascii", "ignore").decode("ascii")
        )
        cleaned = _UNSAFE_FILENAME_CHARS.sub("_", normalized_fallback)
        cleaned = _REPEATED_UNDERSCORES.sub("_", cleaned).strip(" ._-") or "media"

    suffix = ""
    stem = cleaned
    if "." in cleaned:
        possible_stem, possible_suffix = cleaned.rsplit(".", 1)
        if possible_stem and 0 < len(possible_suffix) <= 16:
            stem, suffix = possible_stem, f".{possible_suffix}"

    if stem.split(".", 1)[0].upper() in _WINDOWS_RESERVED_STEMS:
        stem = f"_{stem}"

    if len(stem) + len(suffix) > max_length:
        if len(suffix) >= max_length:
            suffix = suffix[: max(0, max_length - 1)]
        stem = stem[: max(1, max_length - len(suffix))]

    result = f"{stem}{suffix}".rstrip(" .")
    return result or "media"


def _system_dns_resolver(hostname: str, port: int) -> Iterable[DNSResult]:
    return socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)


def _address_from_dns_result(result: DNSResult) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    candidate: object = result
    if isinstance(result, tuple):
        # socket.getaddrinfo returns (..., sockaddr), where sockaddr[0] is the
        # numeric address.  Accept a one-item tuple too for simple test doubles.
        if len(result) >= 5 and isinstance(result[4], tuple) and result[4]:
            candidate = result[4][0]
        elif result:
            candidate = result[0]
    if isinstance(candidate, bytes):
        candidate = candidate.decode("ascii", errors="strict")
    try:
        return ipaddress.ip_address(candidate)  # type: ignore[arg-type]
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise SecurityError("DNS returned a non-IP address for the media host.") from exc


def _address_is_disallowed(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            address.is_loopback,
            address.is_private,
            address.is_link_local,
            address.is_reserved,
            address.is_multicast,
            address.is_unspecified,
            not address.is_global,
        )
    )


def ensure_public_dns_results(results: Iterable[DNSResult]) -> list[DNSResult]:
    """Return DNS results only when every address is globally routable.

    Use this at connection time, not only during URL preflight, so redirects,
    extractor-discovered URLs, and DNS rebinding cannot reach local networks.
    """

    materialized = list(results)
    if not materialized:
        raise SecurityError("The media host did not resolve to an address.")
    addresses = [_address_from_dns_result(result) for result in materialized]
    if any(_address_is_disallowed(address) for address in addresses):
        raise SecurityError("A media request resolved to a non-public address.")
    return materialized


def _parse_url(value: str) -> SplitResult:
    safe_reference = redact_url(value)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise SecurityError(f"Invalid media URL: {safe_reference}") from None

    if parts.scheme.lower() not in {"http", "https"}:
        raise SecurityError("Media URLs must use http or https.")
    if not parts.netloc or parts.hostname is None:
        raise SecurityError(f"Media URL has no host: {safe_reference}")
    if parts.username is not None or parts.password is not None:
        raise SecurityError(f"Credentials are not allowed in media URLs: {safe_reference}")
    try:
        _ = parts.port
    except ValueError:
        raise SecurityError(f"Media URL has an invalid port: {safe_reference}") from None
    return parts


def validate_url(value: str, *, dns_resolver: DNSResolver | None = None) -> str:
    """Validate an HTTP(S) URL and its initial DNS answers.

    All returned addresses must be public.  The original URL is returned for
    use by the downloader; callers must use :func:`redact_url` for metadata or
    errors.

    The download worker supplements this preflight with connection-time checks.
    """

    if not isinstance(value, str) or not value.strip():
        raise SecurityError("A non-empty media URL is required.")

    parts = _parse_url(value)
    hostname = (parts.hostname or "").rstrip(".")
    safe_reference = redact_url(value)
    if not hostname or hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        raise SecurityError(f"Localhost is not allowed as a media source: {safe_reference}")

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        raise SecurityError(f"Media URL has an invalid host: {safe_reference}") from None

    try:
        literal_address = ipaddress.ip_address(ascii_hostname.split("%", 1)[0])
    except ValueError:
        literal_address = None

    if literal_address is not None:
        addresses = [literal_address]
    else:
        resolver = dns_resolver or _system_dns_resolver
        try:
            results = list(
                resolver(
                    ascii_hostname, parts.port or (443 if parts.scheme.lower() == "https" else 80)
                )
            )
        except Exception:
            raise SecurityError(f"Could not resolve media host: {safe_reference}") from None
        if not results:
            raise SecurityError(f"Media host did not resolve: {safe_reference}")
        addresses = [_address_from_dns_result(result) for result in results]

    try:
        ensure_public_dns_results(addresses)
    except SecurityError:
        raise SecurityError(
            f"Media URL resolves to a non-public address: {safe_reference}"
        ) from None

    return value


__all__ = [
    "DNSResolver",
    "ensure_public_dns_results",
    "redact_url",
    "sanitize_filename",
    "validate_url",
]
