"""Command-line interface for Transcribe Anything."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import ConfigurationError, TranscribeAnythingError
from .pipeline import transcribe
from .settings import SUPPORTED_PROVIDERS, Settings

MODELS = (
    "gpt-4o-transcribe-diarize",
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "whisper-1",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcribe-anything",
        description="Transcribe a local audio/video file or accessible public media URL.",
    )
    parser.add_argument("source", help="Local media path or public http(s) URL")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Artifact directory (default: TRANSCRIBE_ANYTHING_OUTPUT_DIR or ./outputs)",
    )
    parser.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=("txt", "md", "json", "srt", "vtt"),
        help="Output format; repeat to select multiple (default: txt,md,json,srt,vtt)",
    )
    parser.add_argument(
        "--language",
        help="Optional input language hint, such as en, es, or en-US",
    )
    parser.add_argument(
        "--provider",
        choices=tuple(sorted(SUPPORTED_PROVIDERS)),
        help="Transcription provider (default: environment setting or openai)",
    )
    parser.add_argument(
        "--model",
        help="OpenAI model name or configured audio.cpp server model id",
    )
    parser.add_argument(
        "--audiocpp-base-url",
        help="audio.cpp server root URL (default: http://127.0.0.1:8080)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages; still print the final JSON manifest",
    )
    return parser


def _progress(stage: str, current: int, total: int) -> None:
    detail = f" {current}/{total}" if total else ""
    print(f"[{stage}]{detail}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
        selected_provider = args.provider or settings.provider
        if selected_provider == "openai" and args.model and args.model not in MODELS:
            raise ConfigurationError(
                "Unsupported OpenAI transcription model. Expected one of: "
                + ", ".join(MODELS)
                + "."
            )
        result = transcribe(
            args.source,
            output_dir=args.output_dir or settings.output_dir,
            formats=args.formats or ("txt", "md", "json", "srt", "vtt"),
            language=args.language,
            model=args.model,
            provider_name=selected_provider,
            audiocpp_base_url=args.audiocpp_base_url,
            settings=settings,
            progress=None if args.quiet else _progress,
        )
    except (TranscribeAnythingError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    manifest = {
        "source": result.document.source,
        "duration_seconds": result.document.duration_seconds,
        "language": result.document.language,
        "provider": result.document.provider,
        "model": result.document.model,
        "files": {name: str(path) for name, path in result.files.items()},
        "warnings": result.document.warnings,
    }
    # Machine-readable output must survive redirected Windows consoles whose
    # encoding is commonly cp1252. JSON escapes preserve all Unicode exactly.
    print(json.dumps(manifest, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
