from __future__ import annotations

import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from transcribe_anything import media
from transcribe_anything.errors import MediaError


def _write_tone(path: Path, duration: float = 0.35) -> None:
    sample_rate = 16_000
    sample_count = int(sample_rate * duration)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for sample in range(sample_count):
            value = int(8_000 * math.sin(2 * math.pi * 440 * sample / sample_rate))
            frames.extend(struct.pack("<h", value))
        wav.writeframes(frames)


def _usable_ffmpeg() -> str:
    try:
        return media.find_ffmpeg()
    except MediaError as exc:
        pytest.skip(f"FFmpeg is not available for the integration test: {exc}")


@pytest.mark.parametrize("name", ["playlist.m3u", "stream.m3u8", "input.ffconcat", "video.mpd"])
def test_probe_rejects_reference_media_extensions(tmp_path: Path, name: str) -> None:
    source = tmp_path / name
    source.write_text("#EXTM3U\nfile://127.0.0.1/C$/secret.wav\n", encoding="utf-8")
    with pytest.raises(MediaError, match="Playlist and manifest"):
        media.probe_media(source, ffmpeg="not-needed")


def test_probe_rejects_disguised_playlist_content(tmp_path: Path) -> None:
    source = tmp_path / "looks-like-audio.mp3"
    source.write_text("#EXTM3U\nfile://127.0.0.1/C$/secret.wav\n", encoding="utf-8")
    with pytest.raises(MediaError, match="Playlist and manifest content"):
        media.probe_media(source, ffmpeg="not-needed")


@pytest.mark.parametrize(
    "content",
    ["[playlist]\nFile1=file://host/share/a.wav", "v=0\ns=file leak\n"],
)
def test_probe_rejects_other_disguised_reference_content(tmp_path: Path, content: str) -> None:
    source = tmp_path / "looks-like-audio.wav"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(MediaError, match="Playlist and manifest content"):
        media.probe_media(source, ffmpeg="not-needed")


def test_parse_probe_output_with_audio() -> None:
    stderr = """
      Duration: 01:02:03.50, start: 0.000000, bitrate: 128 kb/s
      Stream #0:0: Video: h264, yuv420p
      Stream #0:1: Audio: aac, 48000 Hz, stereo
    """

    result = media._parse_probe_output(stderr)

    assert result.duration_seconds == pytest.approx(3723.5)
    assert result.has_audio is True


def test_parse_probe_output_can_report_no_audio() -> None:
    stderr = """
      Duration: 00:00:02.25, start: 0.000000, bitrate: 128 kb/s
      Stream #0:0: Video: h264, yuv420p
    """

    result = media._parse_probe_output(stderr)

    assert result.duration_seconds == pytest.approx(2.25)
    assert result.has_audio is False


@pytest.mark.parametrize(
    "stderr",
    [
        "Duration: N/A, start: 0.000000",
        "Duration: 00:00:00.00, start: 0.000000",
        "not valid media",
    ],
)
def test_parse_probe_output_rejects_missing_or_zero_duration(stderr: str) -> None:
    with pytest.raises(MediaError, match="duration"):
        media._parse_probe_output(stderr)


def test_probe_wraps_ffmpeg_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.wav"
    source.touch()
    monkeypatch.setattr(media, "find_ffmpeg", lambda ffmpeg=None: "ffmpeg")

    def time_out(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(media, "_run_ffmpeg", time_out)

    with pytest.raises(MediaError, match="timed out"):
        media.probe_media(source)


def test_normalize_rejects_media_without_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.touch()
    monkeypatch.setattr(media, "find_ffmpeg", lambda ffmpeg=None: "ffmpeg")
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda path, ffmpeg=None: media.MediaProbe(2.0, False),
    )

    with pytest.raises(MediaError, match="no audio"):
        media.normalize_and_chunk(source, tmp_path / "chunks")


def test_normalize_rejects_failed_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    source.touch()
    monkeypatch.setattr(media, "find_ffmpeg", lambda ffmpeg=None: "ffmpeg")
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda path, ffmpeg=None: media.MediaProbe(2.0, True),
    )
    monkeypatch.setattr(
        media,
        "_run_ffmpeg",
        lambda argv, *, timeout: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="encoder failed"
        ),
    )

    with pytest.raises(MediaError, match="encoder failed"):
        media.normalize_and_chunk(source, tmp_path / "chunks")


def test_normalize_rejects_success_without_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    source.touch()
    monkeypatch.setattr(media, "find_ffmpeg", lambda ffmpeg=None: "ffmpeg")
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda path, ffmpeg=None: media.MediaProbe(2.0, True),
    )
    monkeypatch.setattr(
        media,
        "_run_ffmpeg",
        lambda argv, *, timeout: subprocess.CompletedProcess(
            argv, 0, stdout="", stderr=""
        ),
    )

    with pytest.raises(MediaError, match="without producing"):
        media.normalize_and_chunk(source, tmp_path / "chunks")


def test_normalize_probes_chunks_and_builds_cumulative_offsets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    source.touch()
    executable = tmp_path / "ffmpeg.exe"
    executable.touch()

    def fake_probe(
        path: str | Path, ffmpeg: str | Path | None = None
    ) -> media.MediaProbe:
        name = Path(path).name
        if name.endswith("00000.mp3"):
            return media.MediaProbe(1.25, True)
        if name.endswith("00001.mp3"):
            return media.MediaProbe(0.75, True)
        return media.MediaProbe(2.0, True)

    def write_chunks(
        argv: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        template = argv[-1]
        Path(template.replace("%05d", "00000")).touch()
        Path(template.replace("%05d", "00001")).touch()
        assert argv[argv.index("-ac") + 1] == "1"
        assert argv[argv.index("-ar") + 1] == "16000"
        assert argv[argv.index("-f") + 1] == "segment"
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(media, "probe_media", fake_probe)
    monkeypatch.setattr(media, "_run_ffmpeg", write_chunks)

    chunks = media.normalize_and_chunk(source, tmp_path / "chunks", ffmpeg=executable)

    assert [chunk.index for chunk in chunks] == [0, 1]
    assert [chunk.duration_seconds for chunk in chunks] == [1.25, 0.75]
    assert [chunk.start_seconds for chunk in chunks] == [0.0, 1.25]


@pytest.mark.parametrize("output_format", ["mp3", "wav"])
def test_probe_and_normalize_real_wav(tmp_path: Path, output_format: str) -> None:
    ffmpeg = _usable_ffmpeg()
    source = tmp_path / "tone.wav"
    _write_tone(source)

    probe = media.probe_media(source, ffmpeg=ffmpeg)
    chunks = media.normalize_and_chunk(
        source,
        tmp_path / "chunks",
        chunk_seconds=0.2,
        output_format=output_format,
        ffmpeg=ffmpeg,
    )

    assert probe.has_audio is True
    assert probe.duration_seconds == pytest.approx(0.35, abs=0.02)
    assert chunks
    assert all(chunk.path.suffix == f".{output_format}" for chunk in chunks)
    assert all(chunk.path.is_file() for chunk in chunks)
    assert all(chunk.duration_seconds > 0 for chunk in chunks)
    assert chunks[0].start_seconds == 0
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.index == previous.index + 1
        assert current.start_seconds == pytest.approx(
            previous.start_seconds + previous.duration_seconds
        )


def test_normalize_can_emit_pcm_wav_chunks(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.mp4"
    source.touch()
    executable = tmp_path / "ffmpeg.exe"
    executable.touch()
    observed = {}

    monkeypatch.setattr(
        media,
        "probe_media",
        lambda *args, **kwargs: media.MediaProbe(1.0, True),
    )

    def write_chunk(argv: list[str], *, timeout: float):
        observed["argv"] = argv
        Path(argv[-1].replace("%05d", "00000")).touch()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(media, "_run_ffmpeg", write_chunk)

    chunks = media.normalize_and_chunk(
        source,
        tmp_path / "chunks",
        output_format="wav",
        ffmpeg=executable,
    )

    assert [item.path.suffix for item in chunks] == [".wav"]
    assert observed["argv"][observed["argv"].index("-c:a") + 1] == "pcm_s16le"
    assert "-b:a" not in observed["argv"]


def test_normalize_preserves_positional_ffmpeg_argument(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.wav"
    source.touch()
    executable = tmp_path / "custom-ffmpeg.exe"
    executable.touch()
    observed = {}

    def find_executable(value):
        observed["ffmpeg"] = value
        return str(executable)

    def write_chunk(argv: list[str], *, timeout: float):
        Path(argv[-1].replace("%05d", "00000")).touch()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(media, "find_ffmpeg", find_executable)
    monkeypatch.setattr(
        media,
        "probe_media",
        lambda *args, **kwargs: media.MediaProbe(1.0, True),
    )
    monkeypatch.setattr(media, "_run_ffmpeg", write_chunk)

    chunks = media.normalize_and_chunk(
        source,
        tmp_path / "chunks",
        240,
        "64k",
        executable,
    )

    assert observed["ffmpeg"] == executable
    assert [item.path.suffix for item in chunks] == [".mp3"]


def test_normalize_rejects_unknown_output_format(tmp_path: Path) -> None:
    source = tmp_path / "source.wav"
    source.touch()

    with pytest.raises(MediaError, match="must be 'mp3' or 'wav'"):
        media.normalize_and_chunk(source, tmp_path / "chunks", output_format="flac")
