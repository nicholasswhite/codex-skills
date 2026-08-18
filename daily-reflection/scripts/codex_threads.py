#!/usr/bin/env python3
"""Read a bounded, sanitized view of Codex task history through App Server.

This helper is intentionally stdout-only. It never opens rollout contents directly,
resumes tasks, writes cursors, or mutates Codex state.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
INTERACTIVE_SOURCE_KINDS = ("cli", "vscode")
APP_SERVER_SOURCE_KIND = "appServer"
SUBAGENT_SOURCE_KINDS = (
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
)
VISIBLE_AGENT_PHASES = ("commentary", "final", "final_answer")
DEFAULT_MAX_TASKS = 50
DEFAULT_MAX_PAGES = 20
DEFAULT_MAX_MESSAGE_CHARS = 8_000
DEFAULT_MAX_TASK_CHARS = 50_000
DEFAULT_MAX_OUTPUT_CHARS = 250_000
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_CONFIG_BYTES = 64 * 1024
MAX_APP_SERVER_LINE_CHARS = 20 * 1024 * 1024
HARD_MAX_TASKS = 100
HARD_MAX_PAGES = 50
HARD_MAX_CWDS = 20
HARD_MAX_TIMEOUT_SECONDS = 120.0
HARD_MAX_MESSAGE_CHARS = 50_000
HARD_MAX_TASK_CHARS = 500_000
HARD_MAX_OUTPUT_CHARS = 2_000_000

SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r'''(?ix)["']?(?:api[_ -]?key|accountkey|authorization|aws_secret_access_key|password|passwd|stripe_secret_key|token|secret)["']?'''
        r'''\s*[:=]\s*["']?(?:Basic\s+|Bearer\s+)?[^\s"',;}{]{8,}'''
    ),
    re.compile(r"(?i)[?&](?:sig|signature|token|key|password)=[^&\s]{8,}"),
)


class CollectorError(RuntimeError):
    """A bounded, user-facing collector failure."""


@dataclass(frozen=True)
class PrivacyConfig:
    excluded_terms: tuple[str, ...] = ()
    excluded_thread_ids: tuple[str, ...] = ()
    redact_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Bounds:
    since: datetime
    until: datetime
    timezone_name: str


class CharacterBudget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self, value: str, *, suffix: str = "...[truncated]") -> tuple[str, bool]:
        remaining = max(0, self.limit - self.used)
        if len(value) <= remaining:
            self.used += len(value)
            return value, False
        if remaining <= len(suffix):
            clipped = suffix[:remaining]
        else:
            clipped = value[: remaining - len(suffix)] + suffix
        self.used += len(clipped)
        return clipped, True


class AppServerClient:
    """Minimal JSON-lines client for stable read-only App Server methods."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.executable = trusted_codex_executable()
        self.timeout = timeout
        self._next_id = 1
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        trusted_cwd = codex_home()
        if not trusted_cwd.is_dir():
            trusted_cwd = Path(self.executable).parent
        try:
            self.process = subprocess.Popen(
                [self.executable, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                cwd=str(trusted_cwd),
            )
        except OSError as exc:
            raise CollectorError(f"Could not start Codex App Server: {exc}") from exc
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "daily-reflection",
                        "title": "Daily Reflection",
                        "version": "1.0.0",
                    },
                    "capabilities": {},
                },
            )
            self._notify("initialized", {})
        except Exception:
            self.close()
            raise

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline(MAX_APP_SERVER_LINE_CHARS + 1)
            if not line:
                break
            if len(line) > MAX_APP_SERVER_LINE_CHARS or not line.endswith("\n"):
                self._responses.put(
                    {"_protocol_error": "App Server response exceeded the local safety limit."}
                )
                self.process.terminate()
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                self._responses.put({"_protocol_error": "Non-JSON App Server output."})
                continue
            if isinstance(payload, dict):
                self._responses.put(payload)

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        # Drain diagnostics so the child cannot block, but do not copy potentially
        # sensitive internal log text into reflection output or user-facing errors.
        while self.process.stderr.read(8_192):
            pass

    def _write(self, payload: Mapping[str, Any]) -> None:
        if self.process.poll() is not None:
            raise CollectorError(
                f"Codex App Server exited unexpectedly with code {self.process.returncode}."
            )
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write({"method": method, "params": dict(params)})

    def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        request_id = self._next_id
        self._next_id += 1
        self._write({"id": request_id, "method": method, "params": dict(params)})
        while True:
            try:
                payload = self._responses.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise CollectorError(f"Timed out waiting for App Server method {method}.") from exc
            if "_protocol_error" in payload:
                raise CollectorError(str(payload["_protocol_error"]))
            if payload.get("id") != request_id:
                # Notifications and unrelated responses are intentionally ignored.
                continue
            if payload.get("error") is not None:
                error = payload["error"]
                if isinstance(error, Mapping):
                    code = error.get("code")
                else:
                    code = None
                suffix = f" (code {code})" if isinstance(code, (str, int)) else ""
                raise CollectorError(f"App Server {method} rejected the request{suffix}.")
            return payload.get("result")

    def list_threads(self, params: Mapping[str, Any]) -> Any:
        return self._request("thread/list", params)

    def read_thread(self, thread_id: str, include_turns: bool) -> Any:
        return self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": include_turns}
        )

    def close(self) -> None:
        process = getattr(self, "process", None)
        if process is None or process.poll() is not None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def __enter__(self) -> "AppServerClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def trusted_codex_executable() -> str:
    """Resolve Codex from explicit PATH entries, never from the project CWD."""
    names = ("codex.exe",) if os.name == "nt" else ("codex",)
    current_project = Path.cwd().resolve()
    windows_roots: tuple[Path, ...] = ()
    if os.name == "nt":
        candidates = []
        local_app_data = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("ProgramFiles")
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Programs" / "OpenAI" / "Codex")
            candidates.append(Path(local_app_data) / "OpenAI" / "Codex")
        candidates.append(Path.home() / ".codex" / "packages" / "standalone" / "releases")
        if program_files:
            candidates.append(Path(program_files) / "OpenAI" / "Codex")
        if program_files_x86:
            candidates.append(Path(program_files_x86) / "OpenAI" / "Codex")
        windows_roots = tuple(
            root.resolve() for root in candidates if root.is_absolute() and root.exists()
        )
    for raw_entry in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_entry.strip():
            continue
        entry = Path(raw_entry.strip().strip('"')).expanduser()
        if not entry.is_absolute():
            continue
        try:
            resolved_entry = entry.resolve(strict=True)
        except OSError:
            continue
        if not resolved_entry.is_dir():
            continue
        for name in names:
            candidate = resolved_entry / name
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if (
                not resolved.is_file()
                or candidate.is_relative_to(current_project)
                or resolved.is_relative_to(current_project)
            ):
                continue
            if os.name == "nt" and not any(
                candidate.is_relative_to(root) or resolved.is_relative_to(root)
                for root in windows_roots
            ):
                continue
            return str(resolved)
    raise CollectorError("Trusted Codex executable was not found in an explicit PATH directory.")


def default_config_path() -> Path:
    return codex_home() / "daily-reflection" / "config.json"


def _validated_string_list(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CollectorError(f"Privacy config field {key!r} must be a list of strings.")
    cleaned = tuple(item.strip() for item in value if item.strip())
    if len(cleaned) > 100 or any(len(item) > 500 for item in cleaned):
        raise CollectorError(f"Privacy config field {key!r} exceeds safe limits.")
    return cleaned


def load_privacy_config(path: Path | None) -> PrivacyConfig:
    selected = path or default_config_path()
    if not selected.exists():
        return PrivacyConfig()
    try:
        if selected.stat().st_size > MAX_CONFIG_BYTES:
            raise CollectorError("Privacy config exceeds 64 KiB.")
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError(f"Could not read privacy config {selected}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise CollectorError("Privacy config must contain one JSON object.")
    return PrivacyConfig(
        excluded_terms=_validated_string_list(payload, "excluded_terms"),
        excluded_thread_ids=_validated_string_list(payload, "excluded_thread_ids"),
        redact_terms=_validated_string_list(payload, "redact_terms"),
    )


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        # App Server timestamps are Unix seconds. Tolerate milliseconds defensively.
        numeric = float(value)
        if numeric > 100_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_explicit_time(value: str, label: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None or not re.search(r"(?:Z|[+-]\d\d:\d\d)$", value.strip()):
        raise CollectorError(f"{label} must be an ISO-8601 timestamp with a UTC offset.")
    return parsed


def build_bounds(args: argparse.Namespace) -> Bounds:
    if args.date:
        if args.since or args.until:
            raise CollectorError("Use --date or --since/--until, not both.")
        if not args.timezone:
            raise CollectorError("--date requires --timezone.")
        try:
            selected_date = date.fromisoformat(args.date)
        except ValueError as exc:
            raise CollectorError("--date must use YYYY-MM-DD format.") from exc
        next_date = selected_date.fromordinal(selected_date.toordinal() + 1)
        if args.timezone.casefold() == "local":
            # A naive astimezone() asks the host OS for the applicable local offset,
            # including the offset change at a daylight-saving boundary.
            start_local = datetime.combine(selected_date, datetime_time.min).astimezone()
            end_local = datetime.combine(next_date, datetime_time.min).astimezone()
        elif re.fullmatch(r"[+-]\d\d:\d\d", args.timezone):
            sign = 1 if args.timezone[0] == "+" else -1
            hours, minutes = (int(part) for part in args.timezone[1:].split(":"))
            if hours > 23 or minutes > 59:
                raise CollectorError("Fixed --timezone offsets must use +/-HH:MM.")
            zone = timezone(sign * timedelta(hours=hours, minutes=minutes))
            start_local = datetime.combine(selected_date, datetime_time.min, tzinfo=zone)
            end_local = datetime.combine(next_date, datetime_time.min, tzinfo=zone)
        else:
            try:
                zone = ZoneInfo(args.timezone)
            except ZoneInfoNotFoundError as exc:
                raise CollectorError(
                    f"Timezone data for {args.timezone!r} is unavailable. "
                    "Use --timezone local, a fixed offset such as -04:00, or "
                    "explicit --since/--until timestamps."
                ) from exc
            start_local = datetime.combine(selected_date, datetime_time.min, tzinfo=zone)
            end_local = datetime.combine(next_date, datetime_time.min, tzinfo=zone)
        return Bounds(start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc), args.timezone)
    if not args.since or not args.until:
        raise CollectorError("Provide either --date with --timezone or both --since and --until.")
    since = parse_explicit_time(args.since, "--since")
    until = parse_explicit_time(args.until, "--until")
    if since >= until:
        raise CollectorError("--since must be earlier than --until.")
    return Bounds(since, until, args.timezone or "offsets from --since/--until")


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def thread_time(thread: Mapping[str, Any]) -> datetime | None:
    # `thread/read` can expose a stale persisted updatedAt while recencyAt reflects
    # the current task activity. `thread/list` generally presents the latter as
    # updatedAt, so prefer recencyAt to keep the two stable read methods consistent.
    return parse_datetime(
        thread.get("recencyAt") or thread.get("updatedAt") or thread.get("createdAt")
    )


def thread_id(thread: Mapping[str, Any]) -> str:
    value = thread.get("id") or thread.get("threadId")
    return str(value) if value is not None else ""


def unwrap_thread(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise CollectorError("App Server returned an invalid thread response.")
    candidate = payload.get("thread", payload)
    if not isinstance(candidate, Mapping):
        raise CollectorError("App Server returned an invalid thread object.")
    return candidate


def normalize_path(value: str) -> str:
    expanded = os.path.abspath(os.path.expanduser(value))
    return os.path.normcase(os.path.normpath(expanded))


def _resolved_local_path(value: str) -> str | None:
    if os.name == "nt":
        windows = value.replace("/", "\\")
        if windows.startswith(("\\\\", "\\?\\", "\\.\\")):
            return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or not candidate.is_dir():
        return None
    try:
        return os.path.normcase(str(candidate.resolve(strict=True)))
    except OSError:
        return None


def allowed_cwd(
    thread: Mapping[str, Any],
    allowed: Sequence[str],
    allowed_roots: Sequence[str] = (),
) -> bool:
    if not allowed and not allowed_roots:
        return True
    current = thread.get("cwd")
    if not isinstance(current, str) or not current:
        return False
    normalized = normalize_path(current)
    if normalized in {normalize_path(item) for item in allowed}:
        return True
    if not allowed_roots:
        return False
    current_physical = _resolved_local_path(current)
    if current_physical is None:
        return False
    for root in allowed_roots:
        root_physical = _resolved_local_path(root)
        if root_physical is None:
            continue
        try:
            if os.path.commonpath((current_physical, root_physical)) == root_physical:
                return True
        except ValueError:
            continue
    return False


def source_kind(thread: Mapping[str, Any]) -> str:
    source = thread.get("source", thread.get("threadSource"))
    if isinstance(source, str):
        normalized = source.casefold()
        for kind in (*INTERACTIVE_SOURCE_KINDS, APP_SERVER_SOURCE_KIND):
            if normalized == kind.casefold():
                return kind
        for kind in SUBAGENT_SOURCE_KINDS:
            if normalized == kind.casefold():
                return "subagent"
        return "unknown"
    if isinstance(source, Mapping):
        lowered_keys = {str(key).casefold() for key in source}
        if "subagent" in lowered_keys:
            return "subagent"
        for discriminant in ("type", "kind", "sourceKind"):
            value = source.get(discriminant)
            if isinstance(value, str):
                synthetic = dict(thread)
                synthetic["source"] = value
                return source_kind(synthetic)
    return "unknown"


def is_subagent(thread: Mapping[str, Any]) -> bool:
    return source_kind(thread) == "subagent"


def is_archived(thread: Mapping[str, Any]) -> bool:
    if isinstance(thread.get("_dailyReflectionArchived"), bool):
        return bool(thread["_dailyReflectionArchived"])
    if isinstance(thread.get("archived"), bool):
        return bool(thread["archived"])
    status = thread.get("status")
    if isinstance(status, str) and status.casefold() == "archived":
        return True
    if isinstance(status, Mapping):
        status_type = status.get("type")
        if isinstance(status_type, str) and status_type.casefold() == "archived":
            return True
    raw_path = thread.get("path")
    if isinstance(raw_path, str):
        normalized = raw_path.replace("\\", "/").casefold()
        return "/archived_sessions/" in f"/{normalized.strip('/')}/"
    return False


def authorized_source_kinds(args: argparse.Namespace) -> list[str]:
    kinds = list(INTERACTIVE_SOURCE_KINDS)
    if args.include_app_server:
        kinds.append(APP_SERVER_SOURCE_KIND)
    if args.include_subagents:
        kinds.extend(SUBAGENT_SOURCE_KINDS)
    return kinds


def in_bounds(thread: Mapping[str, Any], bounds: Bounds) -> bool:
    timestamp = thread_time(thread)
    return timestamp is not None and bounds.since <= timestamp < bounds.until


def matches_exclusion(texts: Iterable[str], config: PrivacyConfig) -> bool:
    needles = tuple(term.casefold() for term in config.excluded_terms)
    if not needles:
        return False
    for text in texts:
        folded = text.casefold()
        if any(needle in folded for needle in needles):
            return True
    return False


def redact_text(text: str, config: PrivacyConfig) -> str:
    output = text
    for literal in config.redact_terms:
        output = re.sub(re.escape(literal), "[REDACTED]", output, flags=re.IGNORECASE)
    for pattern in SECRET_PATTERNS:
        output = pattern.sub("[REDACTED_SECRET]", output)
    return output


def contains_suspicious_secret(texts: Iterable[str]) -> bool:
    return any(pattern.search(text) for text in texts for pattern in SECRET_PATTERNS)


def title_of(thread: Mapping[str, Any]) -> str:
    for key in ("name", "title"):
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "Untitled task"


def safe_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[^A-Za-z0-9_. -]", "", value).strip()
    return cleaned[:40] or None


def safe_metadata(thread: Mapping[str, Any], config: PrivacyConfig) -> dict[str, Any]:
    title = redact_text(title_of(thread), config)
    title = title[:157] + "..." if len(title) > 160 else title
    cwd = thread.get("cwd")
    safe_cwd = redact_text(cwd, config)[:1_000] if isinstance(cwd, str) else None
    result: dict[str, Any] = {
        "id": thread_id(thread),
        "title": title,
        "updatedAt": iso_utc(thread_time(thread)),
        "cwd": safe_cwd,
        "sourceKind": source_kind(thread),
        "archived": is_archived(thread),
    }
    status = safe_status(thread.get("status"))
    if status:
        result["status"] = status
    return result


def _list_page(result: Any) -> tuple[list[Mapping[str, Any]], str | None]:
    if not isinstance(result, Mapping):
        raise CollectorError("App Server returned an invalid thread list response.")
    raw_threads = result.get("data", result.get("threads", []))
    if not isinstance(raw_threads, list):
        raise CollectorError("App Server thread list did not contain an array.")
    threads = [item for item in raw_threads if isinstance(item, Mapping)]
    cursor = result.get("nextCursor") or result.get("cursor")
    return threads, str(cursor) if cursor else None


def _query_variants(args: argparse.Namespace) -> Iterable[tuple[str | None, bool]]:
    cwds = list(args.cwd or [])
    if args.cwd_root or not cwds:
        cwds.append(None)
    archived_values = (False, True) if args.include_archived else (False,)
    for cwd in cwds:
        for archived in archived_values:
            yield cwd, archived


def collect_inventory(
    client: Any,
    args: argparse.Namespace,
    bounds: Bounds,
    config: PrivacyConfig,
) -> dict[str, Any]:
    discovered: dict[str, Mapping[str, Any]] = {}
    pages_read = 0
    partial = False
    omitted = 0
    page_budget_exhausted = False
    for cwd, archived in _query_variants(args):
        if page_budget_exhausted:
            break
        cursor: str | None = None
        for _ in range(args.max_pages):
            if pages_read >= args.max_pages:
                partial = True
                page_budget_exhausted = True
                break
            params: dict[str, Any] = {
                "limit": min(100, max(args.max_tasks * 2, 25)),
                "sortKey": "updated_at",
                "useStateDbOnly": True,
                "archived": archived,
                "sourceKinds": authorized_source_kinds(args),
            }
            if cwd:
                params["cwd"] = cwd
            if cursor:
                params["cursor"] = cursor
            page, cursor = _list_page(client.list_threads(params))
            pages_read += 1
            for thread in page:
                identifier = thread_id(thread)
                if identifier:
                    annotated = dict(thread)
                    annotated["_dailyReflectionArchived"] = archived
                    discovered[identifier] = annotated
            timestamps = [thread_time(item) for item in page]
            known = [item for item in timestamps if item is not None]
            if not cursor or (known and max(known) < bounds.since):
                break

    eligible: list[Mapping[str, Any]] = []
    excluded_ids = set(config.excluded_thread_ids)
    for thread in discovered.values():
        identifier = thread_id(thread)
        if not in_bounds(thread, bounds) or not allowed_cwd(
            thread, args.cwd or [], args.cwd_root or []
        ):
            continue
        if is_archived(thread) and not args.include_archived:
            continue
        if is_subagent(thread) and not args.include_subagents:
            continue
        kind = source_kind(thread)
        if kind == APP_SERVER_SOURCE_KIND and not args.include_app_server:
            continue
        if kind not in (*INTERACTIVE_SOURCE_KINDS, APP_SERVER_SOURCE_KIND, "subagent"):
            continue
        inventory_texts = (title_of(thread), str(thread.get("cwd", "")))
        if identifier in excluded_ids or matches_exclusion(inventory_texts, config):
            omitted += 1
            continue
        eligible.append(thread)
    eligible.sort(key=lambda item: thread_time(item) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    if len(eligible) > args.max_tasks:
        eligible = eligible[: args.max_tasks]
        partial = True
    return {
        "schemaVersion": SCHEMA_VERSION,
        "operation": "inventory",
        "generatedAt": iso_utc(datetime.now(timezone.utc)),
        "scope": scope_payload(args, bounds, config),
        "tasks": [safe_metadata(item, config) for item in eligible],
        "omissions": {
            "taskDetailsOmittedByLocalPolicy": omitted,
            "partialBecauseOfLimits": partial,
            "pagesRead": pages_read,
        },
    }


def _message_text(item: Mapping[str, Any]) -> str:
    item_type = str(item.get("type", ""))
    if item_type == "agentMessage":
        value = item.get("text")
        return value if isinstance(value, str) else ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, Mapping):
            kind = str(part.get("type", "")).casefold()
            value = part.get("text")
            if isinstance(value, str) and kind in {"text", "input_text", "output_text", ""}:
                parts.append(value)
            elif "image" in kind:
                parts.append("[image omitted]")
            elif "audio" in kind:
                parts.append("[audio omitted]")
            elif "file" in kind:
                parts.append("[attachment omitted]")
    return "\n".join(parts)


def visible_agent_phase(item: Mapping[str, Any]) -> str | None:
    raw_phase = item.get("phase")
    if not isinstance(raw_phase, str):
        return None
    normalized = raw_phase.casefold()
    return normalized if normalized in VISIBLE_AGENT_PHASES else None


def _turn_in_bounds(turn: Mapping[str, Any], bounds: Bounds | None) -> bool:
    if bounds is None:
        return True
    started = parse_datetime(turn.get("startedAt"))
    if started is None:
        return False
    return bounds.since <= started < bounds.until


def _turn_items(
    thread: Mapping[str, Any], bounds: Bounds | None = None
) -> Iterable[Mapping[str, Any]]:
    turns = thread.get("turns", [])
    if not isinstance(turns, list):
        return
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        if not _turn_in_bounds(turn, bounds):
            continue
        items = turn.get("items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, Mapping):
                yield item


def visible_texts(thread: Mapping[str, Any], bounds: Bounds) -> Iterable[str]:
    yield title_of(thread)
    cwd = thread.get("cwd")
    if isinstance(cwd, str):
        yield cwd
    for item in _turn_items(thread, bounds):
        item_type = item.get("type")
        if item_type == "agentMessage" and visible_agent_phase(item) is None:
            continue
        if item_type in {"userMessage", "agentMessage"}:
            text = _message_text(item)
            if text:
                yield text
        elif item.get("type") == "fileChange":
            yield from _file_paths(item)


def _file_paths(item: Mapping[str, Any]) -> Iterable[str]:
    changes = item.get("changes", [])
    if not isinstance(changes, list):
        return
    for change in changes:
        if not isinstance(change, Mapping):
            continue
        for key in ("path", "filePath", "file_path"):
            value = change.get(key)
            if isinstance(value, str) and value:
                yield value
                break


def sanitize_thread(
    thread: Mapping[str, Any],
    config: PrivacyConfig,
    args: argparse.Namespace,
    bounds: Bounds,
) -> dict[str, Any]:
    task_budget = CharacterBudget(args.max_task_chars)
    messages: list[dict[str, Any]] = []
    activity = Counter()
    failures = Counter()
    file_paths: set[str] = set()
    reasoning_items = 0
    non_visible_agent_messages = 0
    turns_outside_or_unverifiable = 0
    unknown_items = 0
    truncated_messages = 0
    known_events = {
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "commandExecution",
        "webSearch",
        "subAgentActivity",
        "contextCompaction",
        "plan",
        "todoList",
        "computerToolCall",
        "collabToolCall",
    }
    raw_turns = thread.get("turns", [])
    if isinstance(raw_turns, list):
        turns_outside_or_unverifiable = sum(
            1
            for turn in raw_turns
            if not isinstance(turn, Mapping) or not _turn_in_bounds(turn, bounds)
        )
    for item in _turn_items(thread, bounds):
        kind = str(item.get("type", "unknown"))
        if kind == "reasoning":
            reasoning_items += 1
            continue
        if kind in {"userMessage", "agentMessage"}:
            phase = visible_agent_phase(item) if kind == "agentMessage" else None
            if kind == "agentMessage" and phase is None:
                non_visible_agent_messages += 1
                continue
            raw = _message_text(item)
            if not raw:
                continue
            redacted = redact_text(raw, config)
            per_message = CharacterBudget(args.max_message_chars)
            clipped, first_truncation = per_message.take(redacted)
            clipped, second_truncation = task_budget.take(clipped)
            if first_truncation or second_truncation:
                truncated_messages += 1
            if not clipped:
                continue
            message: dict[str, Any] = {
                "role": "user" if kind == "userMessage" else "assistant",
                "text": clipped,
            }
            if kind == "agentMessage":
                if phase:
                    message["phase"] = phase
            messages.append(message)
            continue
        if kind in known_events:
            activity[kind] += 1
            status = safe_status(item.get("status"))
            if status and any(marker in status.casefold() for marker in ("fail", "error", "declin")):
                failures[kind] += 1
            if kind == "fileChange":
                for value in _file_paths(item):
                    file_paths.add(redact_text(value, config)[:500])
            continue
        unknown_items += 1

    payload = safe_metadata(thread, config)
    payload["messages"] = messages
    payload["activity"] = {
        "eventCounts": dict(sorted(activity.items())),
        "failedEventCounts": dict(sorted(failures.items())),
        "filePaths": sorted(file_paths)[:200],
    }
    payload["omissions"] = {
        "reasoningItems": reasoning_items,
        "nonVisibleAgentMessages": non_visible_agent_messages,
        "turnsOutsideOrUnverifiable": turns_outside_or_unverifiable,
        "unknownItems": unknown_items,
        "truncatedMessages": truncated_messages,
        "filePathsTruncated": max(0, len(file_paths) - 200),
    }
    return payload


def validate_thread_scope(thread: Mapping[str, Any], args: argparse.Namespace, bounds: Bounds) -> None:
    identifier = thread_id(thread) or "unknown"
    if not in_bounds(thread, bounds):
        raise CollectorError(f"Task {identifier} is outside the authorized time window.")
    if not allowed_cwd(thread, args.cwd or [], args.cwd_root or []):
        raise CollectorError(f"Task {identifier} is outside the authorized CWD scope.")
    if is_archived(thread) and not args.include_archived:
        raise CollectorError(f"Task {identifier} is archived but archived tasks were not authorized.")
    if is_subagent(thread) and not args.include_subagents:
        raise CollectorError(f"Task {identifier} is a subagent task but subagents were not authorized.")
    kind = source_kind(thread)
    if kind == APP_SERVER_SOURCE_KIND and not args.include_app_server:
        raise CollectorError(
            f"Task {identifier} is an App Server task but that source was not authorized."
        )
    if kind not in (*INTERACTIVE_SOURCE_KINDS, APP_SERVER_SOURCE_KIND, "subagent"):
        raise CollectorError(f"Task {identifier} is not an authorized interactive source kind.")


def collect_visible(
    client: Any,
    args: argparse.Namespace,
    bounds: Bounds,
    config: PrivacyConfig,
) -> dict[str, Any]:
    excluded_ids = set(config.excluded_thread_ids)
    tasks: list[dict[str, Any]] = []
    omitted = 0
    credential_quarantined = 0
    for identifier in dict.fromkeys(args.thread_id):
        if identifier in excluded_ids:
            omitted += 1
            continue
        metadata = unwrap_thread(client.read_thread(identifier, include_turns=False))
        if thread_id(metadata) != identifier:
            raise CollectorError(f"App Server returned the wrong task for ID {identifier}.")
        validate_thread_scope(metadata, args, bounds)
        metadata_texts = (title_of(metadata), str(metadata.get("cwd", "")))
        if matches_exclusion(metadata_texts, config):
            omitted += 1
            continue
        full = unwrap_thread(client.read_thread(identifier, include_turns=True))
        if thread_id(full) != identifier:
            raise CollectorError(f"App Server returned the wrong task for ID {identifier}.")
        validate_thread_scope(full, args, bounds)
        if matches_exclusion(visible_texts(full, bounds), config):
            omitted += 1
            continue
        if contains_suspicious_secret(visible_texts(full, bounds)):
            omitted += 1
            credential_quarantined += 1
            continue
        tasks.append(sanitize_thread(full, config, args, bounds))
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "operation": "read-visible",
        "generatedAt": iso_utc(datetime.now(timezone.utc)),
        "scope": scope_payload(args, bounds, config),
        "tasks": tasks,
        "omissions": {
            "taskDetailsOmittedByLocalPolicy": omitted,
            "credentialQuarantinedTasks": credential_quarantined,
            "reasoningAlwaysOmitted": True,
        },
    }
    return enforce_output_limit(payload, args.max_output_chars)


def enforce_output_limit(payload: dict[str, Any], limit: int) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    trimmed_messages = 0
    while len(encoded) > limit:
        candidates = [task for task in payload.get("tasks", []) if task.get("messages")]
        if not candidates:
            raise CollectorError("Sanitized metadata exceeds the output safety limit.")
        candidates[-1]["messages"].pop()
        trimmed_messages += 1
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if trimmed_messages:
        payload.setdefault("omissions", {})["messagesTrimmedByOutputLimit"] = trimmed_messages
    return payload


def scope_payload(
    args: argparse.Namespace, bounds: Bounds, config: PrivacyConfig
) -> dict[str, Any]:
    return {
        "since": iso_utc(bounds.since),
        "until": iso_utc(bounds.until),
        "timezone": bounds.timezone_name,
        "cwds": [redact_text(item, config)[:1_000] for item in (args.cwd or [])],
        "cwdRoots": [
            redact_text(item, config)[:1_000] for item in (args.cwd_root or [])
        ],
        "includeArchived": bool(args.include_archived),
        "includeSubagents": bool(args.include_subagents),
        "includeAppServer": bool(args.include_app_server),
    }


def add_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", help="Local calendar date in YYYY-MM-DD format.")
    parser.add_argument(
        "--timezone",
        help="IANA timezone, 'local', or +/-HH:MM; required with --date.",
    )
    parser.add_argument("--since", help="Inclusive ISO-8601 timestamp with UTC offset.")
    parser.add_argument("--until", help="Exclusive ISO-8601 timestamp with UTC offset.")
    parser.add_argument("--cwd", action="append", help="Authorized exact task CWD; repeatable.")
    parser.add_argument(
        "--cwd-root",
        action="append",
        help="Authorized existing local project root; descendant task CWDs are included.",
    )
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--include-subagents", action="store_true")
    parser.add_argument(
        "--include-app-server",
        action="store_true",
        help="Include programmatically created App Server tasks.",
    )
    parser.add_argument("--config", type=Path, help="Optional privacy config JSON path.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory or sanitize explicitly authorized Codex task history."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="List bounded task metadata only.")
    add_scope_arguments(inventory)
    inventory.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    inventory.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)

    visible = subparsers.add_parser(
        "read-visible", help="Read sanitized visible messages for explicit task IDs."
    )
    add_scope_arguments(visible)
    visible.add_argument("--thread-id", action="append", required=True)
    visible.add_argument("--max-message-chars", type=int, default=DEFAULT_MAX_MESSAGE_CHARS)
    visible.add_argument("--max-task-chars", type=int, default=DEFAULT_MAX_TASK_CHARS)
    visible.add_argument("--max-output-chars", type=int, default=DEFAULT_MAX_OUTPUT_CHARS)
    return parser


def validate_limits(args: argparse.Namespace) -> None:
    if args.timeout <= 0 or args.timeout > HARD_MAX_TIMEOUT_SECONDS:
        raise CollectorError(
            f"--timeout must be between 0 and {HARD_MAX_TIMEOUT_SECONDS}."
        )
    if len(args.cwd or []) + len(args.cwd_root or []) > HARD_MAX_CWDS:
        raise CollectorError(f"At most {HARD_MAX_CWDS} CWD scopes may be requested.")
    for root in args.cwd_root or []:
        if _resolved_local_path(root) is None:
            raise CollectorError("Each --cwd-root must be an existing local directory.")
    if args.command == "inventory":
        if args.max_tasks <= 0 or args.max_tasks > HARD_MAX_TASKS:
            raise CollectorError(f"--max-tasks must be between 1 and {HARD_MAX_TASKS}.")
        if args.max_pages <= 0 or args.max_pages > HARD_MAX_PAGES:
            raise CollectorError(f"--max-pages must be between 1 and {HARD_MAX_PAGES}.")
    else:
        if len(args.thread_id) > DEFAULT_MAX_TASKS:
            raise CollectorError(f"At most {DEFAULT_MAX_TASKS} task IDs may be read at once.")
        ceilings = {
            "max_message_chars": HARD_MAX_MESSAGE_CHARS,
            "max_task_chars": HARD_MAX_TASK_CHARS,
            "max_output_chars": HARD_MAX_OUTPUT_CHARS,
        }
        for key, ceiling in ceilings.items():
            value = getattr(args, key)
            if value <= 0 or value > ceiling:
                raise CollectorError(
                    f"--{key.replace('_', '-')} must be between 1 and {ceiling}."
                )


def run(argv: Sequence[str] | None = None, client_factory: Any = AppServerClient) -> dict[str, Any]:
    args = build_parser().parse_args(argv)
    validate_limits(args)
    bounds = build_bounds(args)
    config = load_privacy_config(args.config)
    with client_factory(timeout=args.timeout) as client:
        if args.command == "inventory":
            return collect_inventory(client, args, bounds, config)
        return collect_visible(client, args, bounds, config)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        payload = run(argv)
    except CollectorError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
