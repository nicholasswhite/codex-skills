import pytest

from transcribe_anything.errors import ConfigurationError
from transcribe_anything.settings import Settings


def test_environment_defaults_to_openai(monkeypatch):
    monkeypatch.delenv("TRANSCRIBE_ANYTHING_PROVIDER", raising=False)
    monkeypatch.delenv("TRANSCRIBE_ANYTHING_MODEL", raising=False)
    monkeypatch.delenv("TRANSCRIBE_ANYTHING_AUDIOCPP_BASE_URL", raising=False)

    settings = Settings.from_env()

    assert settings.provider == "openai"
    assert settings.model == "gpt-4o-transcribe-diarize"
    assert settings.audiocpp_base_url == "http://127.0.0.1:8080"


def test_environment_uses_audiocpp_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_ANYTHING_PROVIDER", " AUDIOCPP ")
    monkeypatch.delenv("TRANSCRIBE_ANYTHING_MODEL", raising=False)
    monkeypatch.setenv(
        "TRANSCRIBE_ANYTHING_AUDIOCPP_BASE_URL",
        "http://127.0.0.1:9000",
    )

    settings = Settings.from_env()

    assert settings.provider == "audiocpp"
    assert settings.model == "qwen3-asr"
    assert settings.audiocpp_base_url == "http://127.0.0.1:9000"


def test_environment_normalizes_audiocpp_v1_base_url(monkeypatch):
    monkeypatch.setenv(
        "TRANSCRIBE_ANYTHING_AUDIOCPP_BASE_URL",
        "HTTP://localhost:8080/v1/",
    )

    assert Settings.from_env().audiocpp_base_url == "http://localhost:8080"


def test_environment_rejects_unknown_provider(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_ANYTHING_PROVIDER", "unknown")

    with pytest.raises(ConfigurationError, match="must be one of"):
        Settings.from_env()


def test_environment_rejects_empty_model(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_ANYTHING_MODEL", " ")

    with pytest.raises(ConfigurationError, match="must not be empty"):
        Settings.from_env()


@pytest.mark.parametrize(
    "value",
    [
        "ftp://localhost:8080",
        "http://user:secret@localhost:8080",
        "http://localhost:8080?token=secret",
    ],
)
def test_environment_rejects_unsafe_audiocpp_base_url(monkeypatch, value):
    monkeypatch.setenv("TRANSCRIBE_ANYTHING_AUDIOCPP_BASE_URL", value)

    with pytest.raises(ConfigurationError, match="audio.cpp base URL"):
        Settings.from_env()
