import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import Request

import pytest

from transcribe_anything import provider as provider_module
from transcribe_anything.errors import ProviderError
from transcribe_anything.media import AudioChunk
from transcribe_anything.provider import (
    AudioCppProvider,
    OpenAIProvider,
    _default_audiocpp_transport,
)


class FakeTranscriptions:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def fake_client(responses):
    transcriptions = FakeTranscriptions(responses)
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions)), transcriptions


def chunk(tmp_path: Path, index: int = 0, start: float = 0.0) -> AudioChunk:
    path = tmp_path / f"chunk-{index}.mp3"
    path.write_bytes(b"fake audio")
    return AudioChunk(index=index, path=path, start_seconds=start, duration_seconds=12.5)


class FakeAudioCppTransport:
    def __init__(self, post_responses=None):
        self.post_responses = iter(post_responses or [])
        self.requests = []

    def __call__(self, request, timeout_seconds):
        self.requests.append((request, timeout_seconds))
        path = urlsplit(request.full_url).path
        if path == "/health":
            payload = {"status": "ok"}
        elif path == "/v1/models":
            payload = {"data": [{"id": "qwen3-asr", "task": "asr"}]}
        elif path == "/v1/audio/transcriptions":
            payload = next(self.post_responses)
            if isinstance(payload, Exception):
                raise payload
        else:  # pragma: no cover - makes unexpected requests obvious
            raise AssertionError(f"Unexpected audio.cpp request: {path}")
        return json.dumps(payload).encode()


def wav_chunk(
    tmp_path: Path,
    index: int = 0,
    start: float = 0.0,
    duration: float = 12.5,
) -> AudioChunk:
    path = tmp_path / f"chunk-{index}.wav"
    path.write_bytes(b"RIFF fake wav bytes")
    return AudioChunk(
        index=index,
        path=path,
        start_seconds=start,
        duration_seconds=duration,
    )


def test_diarized_response_offsets_segments(tmp_path):
    client, calls = fake_client(
        [
            {
                "text": "Hello there.",
                "segments": [
                    {"start": 1, "end": 3, "text": "Hello there.", "speaker": "A"}
                ],
            }
        ]
    )
    provider = OpenAIProvider(model="gpt-4o-transcribe-diarize", client=client)

    result = provider.transcribe([chunk(tmp_path, start=30)], language="en")

    assert result.text == "Hello there."
    assert result.segments[0].start_seconds == 31
    assert result.segments[0].end_seconds == 33
    assert result.segments[0].speaker == "A"
    assert calls.calls[0]["response_format"] == "diarized_json"
    assert calls.calls[0]["chunking_strategy"] == "auto"
    assert calls.calls[0]["language"] == "en"


def test_multi_chunk_diarization_scopes_speaker_labels(tmp_path):
    client, _ = fake_client(
        [
            {"text": "One", "segments": [{"start": 0, "end": 1, "text": "One", "speaker": "A"}]},
            {"text": "Two", "segments": [{"start": 0, "end": 1, "text": "Two", "speaker": "A"}]},
        ]
    )
    provider = OpenAIProvider(model="gpt-4o-transcribe-diarize", client=client)

    result = provider.transcribe(
        [chunk(tmp_path, index=0), chunk(tmp_path, index=1, start=12.5)]
    )

    assert [segment.speaker for segment in result.segments] == ["Chunk 1 / A", "Chunk 2 / A"]
    assert "scoped to each audio chunk" in result.warnings[0]


def test_json_response_gets_approximate_chunk_segment(tmp_path):
    client, _ = fake_client([SimpleNamespace(text="A transcript")])
    provider = OpenAIProvider(model="gpt-4o-mini-transcribe", client=client)

    result = provider.transcribe([chunk(tmp_path, start=5)])

    assert result.segments[0].start_seconds == 5
    assert result.segments[0].end_seconds == 17.5
    assert "approximate boundaries" in result.warnings[0]


def test_whisper_requests_segment_timestamps(tmp_path):
    client, calls = fake_client([{"text": "Hi", "segments": []}])
    provider = OpenAIProvider(model="whisper-1", client=client)

    provider.transcribe([chunk(tmp_path)])

    assert calls.calls[0]["response_format"] == "verbose_json"
    assert calls.calls[0]["timestamp_granularities"] == ["segment"]
    assert "chunking_strategy" not in calls.calls[0]


def test_provider_wraps_sdk_errors(tmp_path):
    secret = "https://api.example.test?token=super-secret C:\\private\\audio.wav"
    client, _ = fake_client([RuntimeError(secret)])
    provider = OpenAIProvider(client=client)

    with pytest.raises(ProviderError, match="chunk 1 of 1") as captured:
        provider.transcribe([chunk(tmp_path)])
    assert "super-secret" not in str(captured.value)
    assert "private" not in str(captured.value)


def test_provider_rejects_empty_chunks():
    client, _ = fake_client([])
    provider = OpenAIProvider(client=client)

    with pytest.raises(ProviderError, match="No normalized audio"):
        provider.transcribe([])


def test_audiocpp_checks_model_and_offsets_approximate_segments(tmp_path):
    transport = FakeAudioCppTransport([{"text": "One"}, {"text": "Two"}])
    provider = AudioCppProvider(
        model="qwen3-asr",
        base_url="http://127.0.0.1:8080/v1/",
        transport=transport,
    )
    events = []

    result = provider.transcribe(
        [
            wav_chunk(tmp_path, index=0, start=0, duration=4),
            wav_chunk(tmp_path, index=1, start=4, duration=3),
        ],
        language=" en-US ",
        progress=lambda *event: events.append(event),
    )

    assert result.text == "One\nTwo"
    assert [(item.start_seconds, item.end_seconds) for item in result.segments] == [
        (0, 4),
        (4, 7),
    ]
    assert result.language == "en-US"
    assert len(result.warnings) == 1
    assert "approximate boundaries" in result.warnings[0]
    assert events == [("transcribing", 1, 2), ("transcribing", 2, 2)]
    assert [urlsplit(item[0].full_url).path for item in transport.requests] == [
        "/health",
        "/v1/models",
        "/v1/audio/transcriptions",
        "/v1/audio/transcriptions",
    ]
    post_request = transport.requests[2][0]
    assert post_request.method == "POST"
    assert 'boundary=transcribe-anything-' in post_request.headers["Content-type"]
    assert b'name="model"\r\n\r\nqwen3-asr' in post_request.data
    assert b'name="language"\r\n\r\nen-US' in post_request.data
    assert b'filename="audio.wav"' in post_request.data
    assert b"RIFF fake wav bytes" in post_request.data
    assert b"response_format" not in post_request.data
    assert b"chunking_strategy" not in post_request.data


def test_audiocpp_rejects_unconfigured_model_before_upload(tmp_path):
    transport = FakeAudioCppTransport([{"text": "unused"}])

    def missing_model(request, timeout_seconds):
        transport.requests.append((request, timeout_seconds))
        path = urlsplit(request.full_url).path
        payload = {"status": "ok"} if path == "/health" else {"data": []}
        return json.dumps(payload).encode()

    provider = AudioCppProvider(model="missing", transport=missing_model)

    with pytest.raises(ProviderError, match='model "missing"'):
        provider.transcribe([wav_chunk(tmp_path)])
    assert [urlsplit(item[0].full_url).path for item in transport.requests] == [
        "/health",
        "/v1/models",
    ]


def test_audiocpp_rejects_non_asr_model_before_upload(tmp_path):
    requests = []

    def tts_model(request, timeout_seconds):
        requests.append(request)
        path = urlsplit(request.full_url).path
        payload = (
            {"status": "ok"}
            if path == "/health"
            else {"data": [{"id": "voice", "task": "tts"}]}
        )
        return json.dumps(payload).encode()

    provider = AudioCppProvider(model="voice", transport=tts_model)

    with pytest.raises(ProviderError, match='task "tts", not ASR'):
        provider.transcribe([wav_chunk(tmp_path)])
    assert [urlsplit(item.full_url).path for item in requests] == [
        "/health",
        "/v1/models",
    ]


def test_audiocpp_wraps_server_errors_without_secrets(tmp_path):
    secret = "https://server.test?token=super-secret C:\\private\\audio.wav"
    transport = FakeAudioCppTransport([RuntimeError(secret)])
    provider = AudioCppProvider(transport=transport)

    with pytest.raises(ProviderError, match="chunk 1 of 1") as captured:
        provider.transcribe([wav_chunk(tmp_path)])
    assert "super-secret" not in str(captured.value)
    assert "private" not in str(captured.value)


def test_audiocpp_rejects_empty_or_non_wav_chunks(tmp_path):
    transport = FakeAudioCppTransport([])
    provider = AudioCppProvider(transport=transport)

    with pytest.raises(ProviderError, match="No normalized audio"):
        provider.transcribe([])
    with pytest.raises(ProviderError, match="requires normalized WAV"):
        provider.transcribe([chunk(tmp_path)])


def test_audiocpp_loopback_transport_bypasses_ambient_proxy(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    def fail_proxy_aware_open(*args, **kwargs):
        raise AssertionError("loopback request used the proxy-aware opener")

    monkeypatch.setattr(provider_module, "urlopen", fail_proxy_aware_open)
    try:
        request = Request(f"http://127.0.0.1:{server.server_port}/health")
        payload = _default_audiocpp_transport(request, 2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert json.loads(payload) == {"status": "ok"}
