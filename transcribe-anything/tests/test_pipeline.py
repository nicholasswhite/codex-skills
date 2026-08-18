from pathlib import Path

import pytest

from transcribe_anything import pipeline as pipeline_module
from transcribe_anything.errors import ConfigurationError, OutputError
from transcribe_anything.media import AudioChunk, MediaProbe
from transcribe_anything.pipeline import transcribe
from transcribe_anything.provider import ProviderTranscript
from transcribe_anything.schema import TranscriptSegment
from transcribe_anything.settings import Settings
from transcribe_anything.sources import ResolvedSource


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def transcribe(self, chunks, *, language=None, progress=None):
        chunks = list(chunks)
        if progress:
            progress("transcribing", 1, len(chunks))
        return ProviderTranscript(
            text="Hello from the test.",
            segments=[TranscriptSegment(0, 1.5, "Hello from the test.", "A")],
            language=language or "en",
        )


def settings(tmp_path: Path) -> Settings:
    return Settings(
        output_dir=tmp_path / "outputs",
        model="unused",
        max_source_bytes=1024 * 1024,
        chunk_seconds=60,
        ffmpeg=None,
        host="127.0.0.1",
        port=8765,
    )


def test_pipeline_writes_requested_artifacts(monkeypatch, tmp_path):
    media = tmp_path / "clip.wav"
    media.write_bytes(b"media")

    monkeypatch.setattr(
        "transcribe_anything.pipeline.resolve_source",
        lambda *args, **kwargs: ResolvedSource(media, "file", "clip.wav", str(media)),
    )
    monkeypatch.setattr(
        "transcribe_anything.pipeline.probe_media",
        lambda *args, **kwargs: MediaProbe(duration_seconds=2.0, has_audio=True),
    )
    monkeypatch.setattr(
        "transcribe_anything.pipeline.normalize_and_chunk",
        lambda *args, **kwargs: [AudioChunk(0, media, 0.0, 2.0)],
    )
    events = []

    result = transcribe(
        media,
        output_dir=tmp_path / "done",
        formats=("txt", "json", "srt"),
        settings=settings(tmp_path),
        provider=FakeProvider(),
        progress=lambda *event: events.append(event),
    )

    assert set(result.files) == {"txt", "json", "srt"}
    assert all(path.exists() for path in result.files.values())
    assert result.document.source["sha256"]
    assert result.document.provider == "fake"
    assert any(event[0] == "transcribing" for event in events)


def test_pipeline_rejects_unknown_format_before_work(tmp_path):
    with pytest.raises(OutputError, match="Unsupported"):
        transcribe(
            "unused.wav",
            output_dir=tmp_path,
            formats=("docx",),
            settings=settings(tmp_path),
            provider=FakeProvider(),
        )


def test_pipeline_does_not_overwrite_source_that_matches_output(monkeypatch, tmp_path):
    source = tmp_path / "recording.txt"
    source.write_bytes(b"original media bytes")
    monkeypatch.setattr(
        "transcribe_anything.pipeline.resolve_source",
        lambda *args, **kwargs: ResolvedSource(source, "file", source.name, str(source)),
    )
    monkeypatch.setattr(
        "transcribe_anything.pipeline.probe_media",
        lambda *args, **kwargs: MediaProbe(duration_seconds=2.0, has_audio=True),
    )
    monkeypatch.setattr(
        "transcribe_anything.pipeline.normalize_and_chunk",
        lambda *args, **kwargs: [AudioChunk(0, source, 0.0, 2.0)],
    )

    result = transcribe(
        source,
        output_dir=tmp_path,
        formats=("txt",),
        settings=settings(tmp_path),
        provider=FakeProvider(),
    )

    assert source.read_bytes() == b"original media bytes"
    assert result.files["txt"].name == "recording-transcript.txt"


def test_pipeline_caps_manually_constructed_chunk_setting(monkeypatch, tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"media")
    observed = {}
    configured = settings(tmp_path)
    configured = Settings(
        output_dir=configured.output_dir,
        model=configured.model,
        max_source_bytes=configured.max_source_bytes,
        chunk_seconds=900,
        ffmpeg=configured.ffmpeg,
        host=configured.host,
        port=configured.port,
    )
    monkeypatch.setattr(
        "transcribe_anything.pipeline.resolve_source",
        lambda *args, **kwargs: ResolvedSource(source, "file", "clip.wav", str(source)),
    )
    monkeypatch.setattr(
        "transcribe_anything.pipeline.probe_media",
        lambda *args, **kwargs: MediaProbe(duration_seconds=2.0, has_audio=True),
    )

    def observe_chunks(*args, **kwargs):
        observed["seconds"] = kwargs["chunk_seconds"]
        observed["format"] = kwargs["output_format"]
        return [AudioChunk(0, source, 0.0, 2.0)]

    monkeypatch.setattr("transcribe_anything.pipeline.normalize_and_chunk", observe_chunks)
    transcribe(
        source,
        output_dir=tmp_path / "out",
        formats=("txt",),
        settings=configured,
        provider=FakeProvider(),
    )
    assert observed["seconds"] == 240
    assert observed["format"] == "mp3"


def test_environment_rejects_chunk_setting_above_provider_safe_cap(monkeypatch):
    monkeypatch.setenv("TRANSCRIBE_ANYTHING_CHUNK_SECONDS", "241")
    with pytest.raises(ConfigurationError, match="no greater than 240"):
        Settings.from_env()


def test_pipeline_builds_audiocpp_provider_and_requests_wav(monkeypatch, tmp_path):
    source = tmp_path / "clip.wav"
    source.write_bytes(b"media")
    captured = {}

    class LocalProvider(FakeProvider):
        audio_format = "wav"

    def build_provider(**kwargs):
        captured.update(kwargs)
        return LocalProvider()

    monkeypatch.setattr(pipeline_module, "AudioCppProvider", build_provider)
    monkeypatch.setattr(
        pipeline_module,
        "resolve_source",
        lambda *args, **kwargs: ResolvedSource(source, "file", source.name, str(source)),
    )
    monkeypatch.setattr(
        pipeline_module,
        "probe_media",
        lambda *args, **kwargs: MediaProbe(duration_seconds=2.0, has_audio=True),
    )

    def observe_normalization(*args, **kwargs):
        captured["output_format"] = kwargs["output_format"]
        return [AudioChunk(0, source, 0.0, 2.0)]

    monkeypatch.setattr(pipeline_module, "normalize_and_chunk", observe_normalization)

    result = transcribe(
        source,
        output_dir=tmp_path / "out",
        formats=("txt",),
        provider_name="audiocpp",
        settings=settings(tmp_path),
    )

    assert captured["model"] == "qwen3-asr"
    assert captured["base_url"] == "http://127.0.0.1:8080"
    assert captured["output_format"] == "wav"
    assert result.document.provider == "fake"


def test_pipeline_rejects_unknown_provider_before_source_work(tmp_path):
    with pytest.raises(ConfigurationError, match="provider must be one of"):
        transcribe(
            "unused.wav",
            output_dir=tmp_path,
            provider_name="unknown",
            settings=settings(tmp_path),
        )
