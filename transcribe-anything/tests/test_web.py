import importlib
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from transcribe_anything.pipeline import TranscriptionResult
from transcribe_anything.schema import TranscriptDocument
from transcribe_anything.settings import Settings

web_module = importlib.import_module("transcribe_anything.web.app")
client = TestClient(
    web_module.app,
    base_url="http://127.0.0.1",
    client=("127.0.0.1", 50000),
)


def fake_settings(tmp_path: Path) -> Settings:
    return Settings(
        output_dir=(tmp_path / "outputs").resolve(),
        model="gpt-4o-transcribe-diarize",
        max_source_bytes=1024 * 1024,
        chunk_seconds=60,
        ffmpeg=None,
        host="127.0.0.1",
        port=8765,
    )


def fake_pipeline(source, *, output_dir, formats, model, **kwargs):
    source_path = Path(source)
    assert source_path.read_bytes() == b"media bytes"
    output_dir = Path(output_dir)
    output = output_dir / "sample.txt"
    output.write_text("[00:00:00.000 --> 00:00:01.000] Hello\n", encoding="utf-8")
    document = TranscriptDocument(
        source={"kind": "file", "name": source_path.name},
        provider="fake",
        model=model,
        duration_seconds=1,
        text="Hello",
    )
    return TranscriptionResult(document=document, files={"txt": output}, output_dir=output_dir)


def test_index_is_served():
    response = client.get("/")
    assert response.status_code == 200
    assert "Transcribe Anything" in response.text
    assert 'id="provider"' in response.text


def test_config_preserves_openai_defaults_and_adds_providers(monkeypatch, tmp_path):
    settings = fake_settings(tmp_path)
    monkeypatch.setattr(web_module.Settings, "from_env", lambda: settings)

    payload = client.get("/api/config").json()

    assert payload["models"] == list(web_module.MODELS)
    assert payload["default_model"] == settings.model
    assert payload["default_provider"] == "openai"
    assert payload["providers"] == ["audiocpp", "openai"]
    assert payload["default_models"]["audiocpp"] == "qwen3-asr"
    assert payload["models_by_provider"]["openai"] == list(web_module.MODELS)


def test_remote_client_is_rejected():
    remote = TestClient(
        web_module.app,
        base_url="http://public.example",
        client=("198.51.100.10", 50000),
    )
    assert remote.get("/").status_code == 403


def test_cross_site_browser_post_is_rejected_before_processing():
    response = client.post(
        "/api/transcribe",
        headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        data={},
    )
    assert response.status_code == 403


def test_loopback_origin_and_non_browser_post_reach_validation():
    local_browser = client.post(
        "/api/transcribe",
        headers={"Origin": "http://127.0.0.1:8765", "Sec-Fetch-Site": "same-origin"},
        data={},
    )
    no_origin = client.post("/api/transcribe", data={})
    assert local_browser.status_code == 400
    assert no_origin.status_code == 400


def test_upload_returns_preview_and_download(monkeypatch, tmp_path):
    settings = fake_settings(tmp_path)
    uploaded_paths = []

    def observe_upload(source, **kwargs):
        uploaded_paths.append(Path(source))
        return fake_pipeline(source, **kwargs)

    monkeypatch.setattr(web_module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(web_module, "transcribe", observe_upload)

    response = client.post(
        "/api/transcribe",
        data={"model": settings.model, "formats": "txt"},
        files={"media_file": ("sample.wav", b"media bytes", "audio/wav")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "Hello" in payload["preview"]
    assert uploaded_paths and not uploaded_paths[0].exists()
    download = client.get(payload["files"]["txt"])
    assert download.status_code == 200
    assert "Hello" in download.text
    assert download.headers["cache-control"] == "no-store"

    job_dir = settings.output_dir / payload["job_id"]
    (job_dir / "unlisted-secret.txt").write_text("private", encoding="utf-8")
    unlisted = client.get(
        f"/api/jobs/{payload['job_id']}/files/unlisted-secret.txt"
    )
    assert unlisted.status_code == 404


def test_requires_exactly_one_source(monkeypatch, tmp_path):
    monkeypatch.setattr(web_module.Settings, "from_env", lambda: fake_settings(tmp_path))

    neither = client.post("/api/transcribe", data={})
    both = client.post(
        "/api/transcribe",
        data={"source_url": "https://example.com/media"},
        files={"media_file": ("sample.wav", b"media bytes", "audio/wav")},
    )

    assert neither.status_code == 400
    assert both.status_code == 400


def test_upload_passes_audiocpp_provider_and_custom_model(monkeypatch, tmp_path):
    settings = fake_settings(tmp_path)
    captured = {}

    def observe(source, **kwargs):
        captured.update(kwargs)
        return fake_pipeline(source, **kwargs)

    monkeypatch.setattr(web_module.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(web_module, "transcribe", observe)

    response = client.post(
        "/api/transcribe",
        data={
            "provider": "audiocpp",
            "model": "custom-local-asr",
            "formats": "txt",
        },
        files={"media_file": ("sample.wav", b"media bytes", "audio/wav")},
    )

    assert response.status_code == 200
    assert captured["provider_name"] == "audiocpp"
    assert captured["model"] == "custom-local-asr"
    assert response.json()["provider"] == "fake"


def test_web_validates_provider_specific_model_rules(monkeypatch, tmp_path):
    monkeypatch.setattr(web_module.Settings, "from_env", lambda: fake_settings(tmp_path))

    unknown_provider = client.post(
        "/api/transcribe",
        data={"provider": "unknown", "model": "anything"},
        files={"media_file": ("sample.wav", b"media bytes", "audio/wav")},
    )
    unknown_openai_model = client.post(
        "/api/transcribe",
        data={"provider": "openai", "model": "not-an-openai-model"},
        files={"media_file": ("sample.wav", b"media bytes", "audio/wav")},
    )

    assert unknown_provider.status_code == 400
    assert "provider" in unknown_provider.json()["detail"]
    assert unknown_openai_model.status_code == 400
    assert "OpenAI" in unknown_openai_model.json()["detail"]


def test_download_rejects_bad_job_id(monkeypatch, tmp_path):
    monkeypatch.setattr(web_module.Settings, "from_env", lambda: fake_settings(tmp_path))
    response = client.get("/api/jobs/not-a-uuid/files/transcript.txt")
    assert response.status_code == 404


def test_cleanup_removes_expired_uuid_job_but_preserves_other_directories(tmp_path):
    settings = fake_settings(tmp_path)
    expired = settings.output_dir / "00000000-0000-0000-0000-000000000001"
    unrelated = settings.output_dir / "keep-me"
    expired.mkdir(parents=True)
    unrelated.mkdir()
    old = time.time() - (settings.retention_hours + 1) * 60 * 60
    os.utime(expired, (old, old))
    os.utime(unrelated, (old, old))

    web_module._cleanup_expired_jobs(settings)

    assert not expired.exists()
    assert unrelated.exists()
