---
name: transcribe-anything
description: Transcribe speech from local audio or video files, user-provided media attachments, direct HTTP(S) media, and accessible public media-page links into TXT, Markdown, JSON, SRT, or VTT artifacts. Use when Codex is asked to transcribe, caption, subtitle, extract spoken words from, or make a written transcript of a recording, video, podcast, meeting, interview, voice note, or social-media link. Supports timestamps and optional speaker labels through the configured engine; does not bypass authentication, paywalls, access controls, or DRM.
---

# Transcribe Anything

Use the bundled launcher to invoke the repository's tested pipeline. Accept one
local file path or one accessible public HTTP(S) URL per run.

## Workflow

1. Confirm that the user supplied exactly one source. Treat an attached file as
   a local path when the host exposes one.
2. Treat the user's supplied source as authorization to process that source.
   Remind them to use only media they may transcribe. Never bypass
   authentication, paywalls, private-resource controls, or DRM.
3. Choose an output directory inside the current workspace. Default to
   `transcripts/` when the user does not specify one.
4. Run `scripts/transcribe.py SOURCE --output-dir OUTPUT`. Add one or more
   `--format` arguments only when the user requests particular formats. Add
   `--language CODE` only for a known ISO-639-1 language hint. Add `--model`
   only when the user asks for a specific supported engine behavior. OpenAI is
   the default provider; when the user explicitly requests their configured
   local audio.cpp server, add `--provider audiocpp` and use its configured
   model ID. Add `--audiocpp-base-url URL` only when the user explicitly
   supplies a non-default endpoint; otherwise let the launcher use its
   environment setting or loopback default.
5. Read the JSON manifest printed by the launcher. Return clickable absolute
   links to every generated artifact and mention warnings or partial results.
6. If execution fails, relay the actionable error. Do not expose query strings,
   credentials, API keys, or temporary-media paths.

Always use the bundled launcher for the transcript. Never manufacture an empty
or inferred transcript, write substitute artifacts manually, or claim success
without a successful JSON manifest. If the provider key, audio.cpp server, or
another prerequisite is missing, stop and report that prerequisite.

## Provider and model selection

- Respect an existing provider configuration. Otherwise use OpenAI and
  `gpt-4o-transcribe-diarize` by default for timestamps and speaker labels.
- Use `gpt-4o-mini-transcribe` when the user prioritizes a lighter general
  transcript over speaker labeling.
- Use `whisper-1` when the user explicitly needs traditional segment-timestamp
  behavior from that model.
- Use `audiocpp` only when the user requests local inference or it is already
  configured. When `TRANSCRIBE_ANYTHING_MODEL` is unset, its provider-specific
  default model ID is `qwen3-asr`; other values must match the running server
  configuration. The standard audio.cpp transcription response is text-only,
  so segment timestamps use approximate WAV chunk boundaries and speaker
  labels are unavailable.

## Limits

Describe URL support as broad and best-effort, not universal. Public-page
extractors may change. Reject private-network URLs, live streams, and selected
formats that are not a native HTTP(S) download. Process one media item, not
playlists, unless the tool is extended and the user explicitly requests it.
Temporary downloads and normalized audio are deleted automatically; generated
transcript artifacts remain in the selected output directory.
