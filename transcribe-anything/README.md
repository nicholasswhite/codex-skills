# Transcribe Anything

Transcribe a local audio/video file or an accessible public media link from a
browser, a CLI, or the bundled Codex skill. The same Python pipeline powers all
three interfaces and can use OpenAI or a local audio.cpp server.

## What it does

- Accepts common audio/video uploads and local file paths.
- Resolves direct HTTP(S) media and public pages whose selected media is a
  native HTTP(S) download supported by `yt-dlp`.
- Extracts and normalizes audio into bounded chunks with FFmpeg.
- Uses OpenAI by default, with optional language hints, timestamps, and speaker
  labels, or an audio.cpp server for local inference.
- Writes TXT, Markdown, canonical JSON, SRT, and VTT artifacts.
- Deletes downloaded source media and normalized chunks after each job.

Public-link support is best-effort. The guarded MVP rejects live streams and
formats such as RTMP, HLS, or DASH that would require a separate network-capable
downloader. The app does not bypass authentication, private access controls,
paywalls, or DRM. Only process media you have the right or permission to
transcribe.

Audio is split into chunks no longer than four minutes to stay comfortably
within provider output limits. For multi-chunk diarized jobs, speaker labels
are deliberately prefixed with the chunk number because identities cannot be
assumed to carry across independent provider requests.

## Windows setup

Python 3.12 is recommended. From PowerShell:

```powershell
.\scripts\setup.ps1
Copy-Item .env.example .env
```

OpenAI remains the default provider. Add your `OPENAI_API_KEY` to `.env` to use
it. The project uses `imageio-ffmpeg` as a bundled FFmpeg fallback, so a
separate system installation is optional.

## Local audio.cpp provider

The optional `audiocpp` provider requires a separately installed and running
[`audiocpp_server`](https://github.com/0xShug0/audio.cpp/blob/main/app/server/README.md)
with at least one ASR model configured. Build or download audio.cpp, install a
supported model using its [ASR guide](https://github.com/0xShug0/audio.cpp/blob/main/docs/asr.md),
and create a minimal `server.json` like this (adjust the backend and model path):

```json
{
  "host": "127.0.0.1",
  "port": 8080,
  "backend": "cpu",
  "models": [
    {
      "id": "qwen3-asr",
      "family": "qwen3_asr",
      "path": "/path/to/models/Qwen3-ASR-0.6B",
      "task": "asr",
      "mode": "offline"
    }
  ]
}
```

Start the server from the audio.cpp repository:

```text
build/bin/audiocpp_server --config server.json
```

Then select it in this project's `.env`:

```dotenv
TRANSCRIBE_ANYTHING_PROVIDER=audiocpp
TRANSCRIBE_ANYTHING_AUDIOCPP_BASE_URL=http://127.0.0.1:8080
TRANSCRIBE_ANYTHING_MODEL=qwen3-asr
```

`TRANSCRIBE_ANYTHING_MODEL` must match an ID in the server's `models` list. If
the model setting is omitted, the default is `qwen3-asr` for audio.cpp and
`gpt-4o-transcribe-diarize` for OpenAI. You can also select the provider for one
CLI run:

```powershell
.\.venv\Scripts\transcribe-anything.exe "C:\media\interview.mp4" `
  --provider audiocpp `
  --audiocpp-base-url http://127.0.0.1:8080 `
  --model qwen3-asr
```

This pipeline normalizes every source into WAV chunks before uploading them to
audio.cpp's standard transcription endpoint. That endpoint returns text but no
segment timestamps or speaker labels, so JSON, SRT, and VTT output use each
chunk's full boundaries as approximate timestamps.

## Local web app

```powershell
.\scripts\run-web.ps1
```

Open <http://127.0.0.1:8765>. Upload a file or paste a public link, choose the
transcript options, and download the result.

The server binds only to localhost by default. URL downloads run in an isolated
worker that permits only public HTTP(S) destinations and rechecks DNS at each
connection. Authentication, multi-user authorization, and an asynchronous job
queue should still be added before deploying it as a public service.

## CLI

```powershell
.\.venv\Scripts\transcribe-anything.exe "C:\media\interview.mp4" --output-dir .\transcripts
.\.venv\Scripts\transcribe-anything.exe "https://example.com/public-video" --format txt --format srt
```

Useful options:

```text
--language en
--provider openai|audiocpp
--model MODEL_NAME_OR_AUDIOCPP_SERVER_ID
--audiocpp-base-url http://127.0.0.1:8080
--format txt|md|json|srt|vtt   (repeatable)
```

The CLI prints a JSON manifest containing absolute artifact paths.

## Codex skill

This directory is both the canonical skill bundle and the project root. Setup
installs a junction under your user-wide Codex skill directory, keeping this
folder as the single source of truth. Its launcher delegates to the project
CLI, ensuring Codex and the web app use identical ingestion, transcription,
and rendering behavior.

Validate it with:

```powershell
$Validator = Join-Path $env:USERPROFILE '.codex\skills\.system\skill-creator\scripts\quick_validate.py'
& '.\.venv\Scripts\python.exe' $Validator `
  '.'
```

## Privacy and retention

Normalized audio chunks derived from the selected source are sent to the
configured transcription provider. With audio.cpp on the loopback address,
those chunks remain on the local machine; a remote audio.cpp base URL sends
them to that server. Temporary downloads, uploads, and normalized chunks are
deleted after the job. Web transcripts remain in `outputs/<job-id>` for 24
hours by default and are then removed during the next app request. CLI
artifacts remain in the selected output directory until you remove them.
Configure the web window with `TRANSCRIBE_ANYTHING_RETENTION_HOURS`.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
```
