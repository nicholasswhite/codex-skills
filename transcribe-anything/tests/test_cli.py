import io
import json
import sys
from types import SimpleNamespace

from transcribe_anything.cli import main
from transcribe_anything.schema import TranscriptDocument


def test_cli_prints_manifest(monkeypatch, tmp_path, capsys):
    output = tmp_path / "clip.txt"
    output.write_text("hello", encoding="utf-8")
    document = TranscriptDocument(
        source={"kind": "file", "name": "clip.wav"},
        provider="fake",
        model="fake-1",
        duration_seconds=1,
        text="hello",
    )
    monkeypatch.setattr(
        "transcribe_anything.cli.transcribe",
        lambda *args, **kwargs: SimpleNamespace(
            document=document, files={"txt": output}
        ),
    )

    assert main(["clip.wav", "--format", "txt", "--quiet"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"]["txt"] == str(output)


def test_cli_returns_one_for_user_error(monkeypatch, capsys):
    from transcribe_anything.errors import SourceError

    def fail(*args, **kwargs):
        raise SourceError("not found")

    monkeypatch.setattr("transcribe_anything.cli.transcribe", fail)
    assert main(["missing.wav", "--quiet"]) == 1
    assert "not found" in capsys.readouterr().err


def test_cli_manifest_is_safe_for_redirected_cp1252_stdout(monkeypatch, tmp_path):
    output = tmp_path / "transcript.txt"
    output.write_text("hello", encoding="utf-8")
    document = TranscriptDocument(
        source={"kind": "file", "reference": "C:/media/🎥.wav"},
        provider="fake",
        model="fake-1",
        duration_seconds=1,
        text="hello",
    )
    monkeypatch.setattr(
        "transcribe_anything.cli.transcribe",
        lambda *args, **kwargs: SimpleNamespace(document=document, files={"txt": output}),
    )
    raw = io.BytesIO()
    redirected = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", redirected)

    assert main(["clip.wav", "--format", "txt", "--quiet"]) == 0
    redirected.flush()
    payload = json.loads(raw.getvalue().decode("cp1252"))
    assert payload["source"]["reference"] == "C:/media/🎥.wav"


def test_cli_passes_audiocpp_selection(monkeypatch, tmp_path, capsys):
    output = tmp_path / "clip.txt"
    output.write_text("hello", encoding="utf-8")
    document = TranscriptDocument(
        source={"kind": "file", "name": "clip.wav"},
        provider="audiocpp",
        model="local-asr",
        duration_seconds=1,
        text="hello",
    )
    captured = {}

    def observe(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(document=document, files={"txt": output})

    monkeypatch.setattr("transcribe_anything.cli.transcribe", observe)

    assert main(
        [
            "clip.wav",
            "--provider",
            "audiocpp",
            "--model",
            "local-asr",
            "--audiocpp-base-url",
            "http://127.0.0.1:9000",
            "--format",
            "txt",
            "--quiet",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured["provider_name"] == "audiocpp"
    assert captured["model"] == "local-asr"
    assert captured["audiocpp_base_url"] == "http://127.0.0.1:9000"
    assert payload["provider"] == "audiocpp"
