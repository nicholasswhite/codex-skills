"""Transcription provider abstraction and cloud/local implementations."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

from .errors import ConfigurationError, ProviderError
from .media import AudioChunk
from .schema import TranscriptSegment
from .settings import normalise_audiocpp_base_url

ProgressCallback = Callable[[str, int, int], None]


@dataclass(slots=True)
class ProviderTranscript:
    text: str
    segments: list[TranscriptSegment]
    language: str | None = None
    warnings: list[str] = field(default_factory=list)


class TranscriptionProvider(Protocol):
    audio_format: str
    model: str
    name: str

    def transcribe(
        self,
        chunks: Iterable[AudioChunk],
        *,
        language: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> ProviderTranscript: ...


def _response_mapping(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        return response
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    data: dict[str, Any] = {}
    for key in ("text", "segments", "language", "duration"):
        value = getattr(response, key, None)
        if value is not None:
            data[key] = value
    return data


def _item_mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return item
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    return {
        key: getattr(item, key, None)
        for key in ("start", "end", "text", "speaker")
    }


class OpenAIProvider:
    """Transcribe normalized chunks through OpenAI's Audio Transcriptions API."""

    audio_format = "mp3"
    name = "openai"

    def __init__(
        self,
        *,
        model: str = "gpt-4o-transcribe-diarize",
        api_key: str | None = None,
        client: Any | None = None,
        timeout_seconds: float = 1800.0,
    ) -> None:
        self.model = model.strip()
        if not self.model:
            raise ConfigurationError("A transcription model is required.")
        if client is not None:
            self._client = client
            return

        resolved_key = api_key or os.getenv("OPENAI_API_KEY")
        if not resolved_key:
            raise ConfigurationError(
                "OPENAI_API_KEY is not configured. Set it in your environment or a local .env file."
            )
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - packaging guards this
            raise ConfigurationError(
                "The OpenAI Python package is missing. Install the project dependencies first."
            ) from exc
        self._client = OpenAI(api_key=resolved_key, timeout=timeout_seconds, max_retries=2)

    def _request_options(self, language: str | None) -> dict[str, Any]:
        options: dict[str, Any] = {"model": self.model}
        if language:
            options["language"] = language.strip().lower()

        if self.model == "gpt-4o-transcribe-diarize":
            options.update(response_format="diarized_json", chunking_strategy="auto")
        elif self.model == "whisper-1":
            options.update(
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )
        else:
            options.update(response_format="json", chunking_strategy="auto")
        return options

    def transcribe(
        self,
        chunks: Iterable[AudioChunk],
        *,
        language: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> ProviderTranscript:
        chunk_list = list(chunks)
        if not chunk_list:
            raise ProviderError("No normalized audio chunks were provided for transcription.")

        all_segments: list[TranscriptSegment] = []
        texts: list[str] = []
        detected_language: str | None = None
        warnings: list[str] = []
        chunk_scoped_speakers = (
            self.model == "gpt-4o-transcribe-diarize" and len(chunk_list) > 1
        )
        if chunk_scoped_speakers:
            warnings.append(
                "Speaker labels are scoped to each audio chunk; the same label does not "
                "guarantee the same person across the full recording."
            )

        for position, chunk in enumerate(chunk_list, start=1):
            if progress:
                progress("transcribing", position, len(chunk_list))
            options = self._request_options(language)
            try:
                with chunk.path.open("rb") as audio_file:
                    response = self._client.audio.transcriptions.create(
                        file=audio_file,
                        **options,
                    )
            except Exception as exc:  # SDK errors share no stable public base across versions
                raise ProviderError(
                    f"OpenAI transcription failed on chunk {position} of {len(chunk_list)}. "
                    "Check the API key, model access, network connection, and provider status."
                ) from exc

            payload = _response_mapping(response)
            response_text = str(payload.get("text") or "").strip()
            if response_text:
                texts.append(response_text)
            if not detected_language and payload.get("language"):
                detected_language = str(payload["language"])

            raw_segments = payload.get("segments") or []
            parsed_count = 0
            for raw in raw_segments:
                segment = _item_mapping(raw)
                text = str(segment.get("text") or "").strip()
                if not text:
                    continue
                try:
                    relative_start = float(segment.get("start") or 0.0)
                    relative_end = float(segment.get("end") or chunk.duration_seconds)
                except (TypeError, ValueError):
                    relative_start = 0.0
                    relative_end = chunk.duration_seconds
                speaker = str(segment["speaker"]) if segment.get("speaker") else None
                if speaker and chunk_scoped_speakers:
                    speaker = f"Chunk {position} / {speaker}"
                all_segments.append(
                    TranscriptSegment(
                        start_seconds=chunk.start_seconds + max(0.0, relative_start),
                        end_seconds=chunk.start_seconds
                        + max(relative_start, relative_end, 0.0),
                        text=text,
                        speaker=speaker,
                    )
                )
                parsed_count += 1

            if parsed_count == 0 and response_text:
                all_segments.append(
                    TranscriptSegment(
                        start_seconds=chunk.start_seconds,
                        end_seconds=chunk.start_seconds + chunk.duration_seconds,
                        text=response_text,
                    )
                )
                if self.model not in {"gpt-4o-transcribe-diarize", "whisper-1"}:
                    warnings.append(
                        "The selected model does not return segment timestamps; "
                        "this chunk uses approximate boundaries."
                    )

        return ProviderTranscript(
            text="\n".join(texts).strip(),
            segments=all_segments,
            language=detected_language or language,
            warnings=list(dict.fromkeys(warnings)),
        )


AudioCppTransport = Callable[[Request, float], bytes]
_NO_PROXY_OPENER = build_opener(ProxyHandler({}))


def _is_loopback_url(value: str) -> bool:
    hostname = urlsplit(value).hostname
    if not hostname:
        return False
    if hostname.rstrip(".").lower() == "localhost":
        return True
    try:
        return ip_address(hostname.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _default_audiocpp_transport(request: Request, timeout_seconds: float) -> bytes:
    if _is_loopback_url(request.full_url):
        response_context = _NO_PROXY_OPENER.open(request, timeout=timeout_seconds)
    else:
        response_context = urlopen(request, timeout=timeout_seconds)  # noqa: S310
    with response_context as response:
        return response.read()


def _multipart_transcription_body(
    *,
    model: str,
    audio_path: Path,
    language: str | None,
) -> tuple[bytes, str]:
    boundary = f"transcribe-anything-{uuid.uuid4().hex}"
    marker = boundary.encode("ascii")
    body = bytearray()

    def field(name: str, value: str) -> None:
        body.extend(b"--" + marker + b"\r\n")
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                "ascii"
            )
        )
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")

    field("model", model)
    if language:
        field("language", language)
    body.extend(b"--" + marker + b"\r\n")
    body.extend(
        b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
    )
    body.extend(b"Content-Type: audio/wav\r\n\r\n")
    body.extend(audio_path.read_bytes())
    body.extend(b"\r\n--" + marker + b"--\r\n")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


class AudioCppProvider:
    """Transcribe WAV chunks through an audio.cpp HTTP server."""

    audio_format = "wav"
    name = "audiocpp"

    def __init__(
        self,
        *,
        model: str = "qwen3-asr",
        base_url: str = "http://127.0.0.1:8080",
        timeout_seconds: float = 1800.0,
        probe_timeout_seconds: float = 10.0,
        transport: AudioCppTransport | None = None,
    ) -> None:
        self.model = model.strip()
        if not self.model:
            raise ConfigurationError("An audio.cpp server model id is required.")
        if "\r" in self.model or "\n" in self.model:
            raise ConfigurationError("The audio.cpp server model id is invalid.")
        if timeout_seconds <= 0 or probe_timeout_seconds <= 0:
            raise ConfigurationError("audio.cpp timeouts must be greater than zero.")
        self.base_url = normalise_audiocpp_base_url(base_url)
        self._timeout_seconds = float(timeout_seconds)
        self._probe_timeout_seconds = float(probe_timeout_seconds)
        self._transport = transport or _default_audiocpp_transport
        self._ready = False

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str | None = None,
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        headers = {"Accept": "application/json", "User-Agent": "transcribe-anything"}
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            raw = self._transport(request, timeout_seconds)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ProviderError(
                "audio.cpp request failed. Verify that the local server is running "
                "and that its base URL is configured correctly."
            ) from exc
        if not isinstance(payload, Mapping):
            raise ProviderError("audio.cpp returned an invalid JSON response.")
        return payload

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        health = self._request_json(
            "/health",
            timeout_seconds=self._probe_timeout_seconds,
        )
        if health.get("status") != "ok":
            raise ProviderError("audio.cpp health check did not report ready status.")
        models = self._request_json(
            "/v1/models",
            timeout_seconds=self._probe_timeout_seconds,
        )
        raw_models = models.get("data")
        if not isinstance(raw_models, list):
            raise ProviderError("audio.cpp returned an invalid model list.")
        model_entries = {
            str(item["id"]): item
            for item in raw_models
            if isinstance(item, Mapping) and item.get("id")
        }
        available = sorted(model_entries)
        selected = model_entries.get(self.model)
        if selected is None:
            detail = ", ".join(available[:10]) or "none"
            raise ProviderError(
                f'audio.cpp model "{self.model}" is not configured on the server. '
                f"Available model ids: {detail}."
            )
        task = str(selected.get("task") or "").strip().lower()
        if task and task != "asr":
            raise ProviderError(
                f'audio.cpp model "{self.model}" is configured for task "{task}", '
                "not ASR."
            )
        self._ready = True

    def transcribe(
        self,
        chunks: Iterable[AudioChunk],
        *,
        language: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> ProviderTranscript:
        chunk_list = list(chunks)
        if not chunk_list:
            raise ProviderError("No normalized audio chunks were provided for transcription.")
        self._ensure_ready()

        clean_language = language.strip() if language else None
        if clean_language and ("\r" in clean_language or "\n" in clean_language):
            raise ProviderError("The transcription language hint is invalid.")
        texts: list[str] = []
        segments: list[TranscriptSegment] = []
        detected_language: str | None = None
        for position, chunk in enumerate(chunk_list, start=1):
            if chunk.path.suffix.lower() != ".wav":
                raise ProviderError("audio.cpp transcription requires normalized WAV chunks.")
            if progress:
                progress("transcribing", position, len(chunk_list))
            try:
                body, content_type = _multipart_transcription_body(
                    model=self.model,
                    audio_path=chunk.path,
                    language=clean_language,
                )
            except OSError as exc:
                raise ProviderError(
                    f"Could not read normalized audio chunk {position} of {len(chunk_list)}."
                ) from exc
            try:
                payload = self._request_json(
                    "/v1/audio/transcriptions",
                    method="POST",
                    body=body,
                    content_type=content_type,
                    timeout_seconds=self._timeout_seconds,
                )
            except ProviderError as exc:
                raise ProviderError(
                    f"audio.cpp transcription failed on chunk {position} of "
                    f"{len(chunk_list)}. Check the local server and model configuration."
                ) from exc
            response_text = str(payload.get("text") or "").strip()
            if response_text:
                texts.append(response_text)
                segments.append(
                    TranscriptSegment(
                        start_seconds=chunk.start_seconds,
                        end_seconds=chunk.start_seconds + chunk.duration_seconds,
                        text=response_text,
                    )
                )
            if not detected_language and payload.get("language"):
                detected_language = str(payload["language"])

        return ProviderTranscript(
            text="\n".join(texts).strip(),
            segments=segments,
            language=detected_language or clean_language,
            warnings=[
                "audio.cpp does not return segment timestamps from its standard "
                "transcription endpoint; non-empty chunks use approximate boundaries."
            ],
        )
