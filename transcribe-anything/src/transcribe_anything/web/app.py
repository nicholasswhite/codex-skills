"""FastAPI localhost UI for uploads and public media links."""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from ..cli import MODELS
from ..errors import ConfigurationError, TranscribeAnythingError
from ..pipeline import transcribe
from ..renderers import SUPPORTED_FORMATS
from ..security import sanitize_filename
from ..settings import DEFAULT_MODELS, SUPPORTED_PROVIDERS, Settings

STATIC_DIR = Path(__file__).with_name("static")
MAX_UPLOAD_CHUNK = 1024 * 1024
JOB_MANIFEST = ".transcribe-job.json"
app = FastAPI(
    title="Transcribe Anything",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _is_loopback(value: str | None) -> bool:
    if not value:
        return False
    if value.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@app.middleware("http")
async def localhost_only(request: Request, call_next):
    peer = request.client.host if request.client else None
    if not _is_loopback(peer) or not _is_loopback(request.url.hostname):
        return JSONResponse(status_code=403, content={"detail": "Local access only."})
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        # Browsers may submit cross-origin forms to localhost even though the
        # same-origin policy prevents reading the response. Reject those writes
        # before they can trigger downloads or provider spend. CLI clients omit
        # Origin and remain supported.
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse(status_code=403, content={"detail": "Cross-site request denied."})
        origin = request.headers.get("origin")
        if origin:
            try:
                origin_parts = urlsplit(origin)
            except ValueError:
                origin_parts = None
            if (
                origin_parts is None
                or origin_parts.scheme.lower() not in {"http", "https"}
                or not _is_loopback(origin_parts.hostname)
            ):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-site request denied."},
                )
    return await call_next(request)


def _safe_job_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Transcript job not found.") from exc


def _job_dir(settings: Settings, job_id: str) -> Path:
    destination = (settings.output_dir / _safe_job_id(job_id)).resolve()
    try:
        destination.relative_to(settings.output_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Transcript job not found.") from exc
    return destination


def _cleanup_expired_jobs(settings: Settings) -> None:
    root = settings.output_dir
    if not root.is_dir():
        return
    cutoff = time.time() - settings.retention_hours * 60 * 60
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            uuid.UUID(child.name)
            expired = child.stat().st_mtime < cutoff
        except (OSError, ValueError):
            continue
        if expired:
            shutil.rmtree(child, ignore_errors=True)


def _write_job_manifest(directory: Path, filenames: list[str]) -> None:
    manifest = directory / JOB_MANIFEST
    temporary = directory / f"{JOB_MANIFEST}.tmp"
    temporary.write_text(
        json.dumps({"files": sorted(filenames)}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, manifest)


def _allowed_job_files(directory: Path) -> set[str]:
    manifest = directory / JOB_MANIFEST
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    files = payload.get("files")
    if not isinstance(files, list):
        return set()
    return {
        value
        for value in files
        if isinstance(value, str) and Path(value).name == value
    }


async def _save_upload(upload: UploadFile, path: Path, max_bytes: int) -> None:
    size = 0
    try:
        with path.open("wb") as output:
            while chunk := await upload.read(MAX_UPLOAD_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(status_code=413, detail="Uploaded media is too large.")
                output.write(chunk)
    finally:
        await upload.close()
    if size == 0:
        raise HTTPException(status_code=400, detail="Uploaded media is empty.")


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def config() -> dict[str, object]:
    settings = Settings.from_env()
    _cleanup_expired_jobs(settings)
    default_models = dict(DEFAULT_MODELS)
    default_models[settings.provider] = settings.model
    return {
        "providers": sorted(SUPPORTED_PROVIDERS),
        "default_provider": settings.provider,
        "models": MODELS,
        "models_by_provider": {"openai": MODELS, "audiocpp": []},
        "default_models": default_models,
        "default_model": settings.model,
        "formats": sorted(SUPPORTED_FORMATS),
        "max_source_mb": settings.max_source_bytes // (1024 * 1024),
        "api_key_configured": bool(os.getenv("OPENAI_API_KEY")),
        "audiocpp_base_url": settings.audiocpp_base_url,
        "retention_hours": settings.retention_hours,
    }


@app.post("/api/transcribe")
async def create_transcript(
    source_url: Annotated[str | None, Form()] = None,
    media_file: Annotated[UploadFile | None, File()] = None,
    provider: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    formats: Annotated[list[str] | None, Form()] = None,
) -> JSONResponse:
    settings = Settings.from_env()
    _cleanup_expired_jobs(settings)
    requested_formats = formats or ["txt", "md", "json", "srt", "vtt"]
    clean_url = (source_url or "").strip()
    selected_provider = (provider or settings.provider).strip().lower()
    if bool(clean_url) == bool(media_file and media_file.filename):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one source: either a media file or a public URL.",
        )
    if selected_provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unsupported transcription provider.")
    default_model = (
        settings.model
        if selected_provider == settings.provider
        else DEFAULT_MODELS[selected_provider]
    )
    selected_model = (model or default_model).strip()
    if not selected_model:
        raise HTTPException(status_code=400, detail="A transcription model is required.")
    if selected_provider == "openai" and selected_model not in MODELS:
        raise HTTPException(status_code=400, detail="Unsupported OpenAI transcription model.")
    invalid_formats = sorted(set(requested_formats) - SUPPORTED_FORMATS)
    if invalid_formats:
        raise HTTPException(status_code=400, detail="Unsupported output format.")

    job_id = str(uuid.uuid4())
    destination = _job_dir(settings, job_id)
    destination.mkdir(parents=True, exist_ok=False)
    upload_dir: Path | None = None

    try:
        source: str | Path
        if media_file and media_file.filename:
            upload_dir = Path(tempfile.mkdtemp(prefix="transcribe-upload-"))
            filename = sanitize_filename(media_file.filename)
            source = upload_dir / filename
            await _save_upload(media_file, source, settings.max_source_bytes)
        else:
            source = clean_url

        result = await run_in_threadpool(
            lambda: transcribe(
                source,
                output_dir=destination,
                formats=requested_formats,
                language=(language or "").strip() or None,
                model=selected_model,
                provider_name=selected_provider,
                settings=settings,
            )
        )
        _write_job_manifest(destination, [path.name for path in result.files.values()])
    except HTTPException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    except (TranscribeAnythingError, OSError, ValueError) as exc:
        shutil.rmtree(destination, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if upload_dir:
            shutil.rmtree(upload_dir, ignore_errors=True)

    files = {
        name: f"/api/jobs/{quote(job_id)}/files/{quote(path.name)}"
        for name, path in result.files.items()
    }
    preview = result.files.get("txt") or result.files.get("md")
    preview_text = preview.read_text(encoding="utf-8") if preview else result.document.text
    return JSONResponse(
        {
            "job_id": job_id,
            "source": result.document.source,
            "duration_seconds": result.document.duration_seconds,
            "language": result.document.language,
            "provider": result.document.provider,
            "model": result.document.model,
            "warnings": result.document.warnings,
            "preview": preview_text,
            "files": files,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/jobs/{job_id}/files/{filename}")
def download(job_id: str, filename: str) -> FileResponse:
    settings = Settings.from_env()
    _cleanup_expired_jobs(settings)
    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Transcript file not found.")
    directory = _job_dir(settings, job_id)
    if filename not in _allowed_job_files(directory):
        raise HTTPException(status_code=404, detail="Transcript file not found.")
    candidate = (directory / filename).resolve()
    try:
        candidate.relative_to(directory)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Transcript file not found.") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Transcript file not found.")
    return FileResponse(
        candidate,
        filename=candidate.name,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.exception_handler(TranscribeAnythingError)
def application_error(_, exc: TranscribeAnythingError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


def main() -> int:
    settings = Settings.from_env()
    if not _is_loopback(settings.host):
        raise ConfigurationError(
            "This MVP web server may bind only to localhost or a loopback address."
        )
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
