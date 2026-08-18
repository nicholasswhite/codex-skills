"""Environment-backed application settings with conservative local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

from .errors import ConfigurationError

MAX_CHUNK_SECONDS = 240
SUPPORTED_PROVIDERS = frozenset({"openai", "audiocpp"})
DEFAULT_MODELS = {
    "openai": "gpt-4o-transcribe-diarize",
    "audiocpp": "qwen3-asr",
}


def normalise_audiocpp_base_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("The audio.cpp base URL is invalid.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(
            "The audio.cpp base URL must be an http(s) server URL."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "The audio.cpp base URL must not include credentials, a query, or a fragment."
        )
    if port is not None and not 1 <= port <= 65535:
        raise ConfigurationError("The audio.cpp base URL port is invalid.")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def _positive_int(name: str, default: int, *, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be no greater than {maximum}.")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    output_dir: Path
    model: str
    max_source_bytes: int
    chunk_seconds: int
    ffmpeg: str | None
    host: str
    port: int
    retention_hours: int = 24
    provider: str = "openai"
    audiocpp_base_url: str = "http://127.0.0.1:8080"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        output_dir = Path(os.getenv("TRANSCRIBE_ANYTHING_OUTPUT_DIR", "outputs"))
        max_mb = _positive_int("TRANSCRIBE_ANYTHING_MAX_SOURCE_MB", 2048)
        provider = os.getenv("TRANSCRIBE_ANYTHING_PROVIDER", "openai").strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            expected = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ConfigurationError(
                f"TRANSCRIBE_ANYTHING_PROVIDER must be one of: {expected}."
            )
        model = os.getenv("TRANSCRIBE_ANYTHING_MODEL", DEFAULT_MODELS[provider]).strip()
        if not model:
            raise ConfigurationError("TRANSCRIBE_ANYTHING_MODEL must not be empty.")
        return cls(
            output_dir=output_dir.expanduser().resolve(),
            model=model,
            max_source_bytes=max_mb * 1024 * 1024,
            chunk_seconds=_positive_int(
                "TRANSCRIBE_ANYTHING_CHUNK_SECONDS",
                MAX_CHUNK_SECONDS,
                maximum=MAX_CHUNK_SECONDS,
            ),
            ffmpeg=os.getenv("TRANSCRIBE_ANYTHING_FFMPEG") or None,
            host=os.getenv("TRANSCRIBE_ANYTHING_HOST", "127.0.0.1"),
            port=_positive_int("TRANSCRIBE_ANYTHING_PORT", 8765),
            retention_hours=_positive_int("TRANSCRIBE_ANYTHING_RETENTION_HOURS", 24),
            provider=provider,
            audiocpp_base_url=normalise_audiocpp_base_url(
                os.getenv(
                    "TRANSCRIBE_ANYTHING_AUDIOCPP_BASE_URL",
                    "http://127.0.0.1:8080",
                )
            ),
        )
