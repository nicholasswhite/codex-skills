#!/usr/bin/env python3
"""Persist sanitized daily-reflection results outside projects and skill sources."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
MAX_INPUT_CHARS = 750_000
MAX_REPORT_CHARS = 500_000
MAX_FRICTION_ENTRIES = 100
MAX_CHECKPOINTS = 200
MAX_FIELD_CHARS = 2_000
MAX_RECENT_FRICTION = 500
MAX_FRICTION_READ_BYTES = 10 * 1024 * 1024
MAX_FRICTION_FILE_BYTES = 100 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 15.0

TOP_LEVEL_FIELDS = {
    "schemaVersion",
    "reflectionDate",
    "timezone",
    "reportMarkdown",
    "friction",
    "taskCheckpoints",
}
FRICTION_FIELDS = {
    "category",
    "skill",
    "severity",
    "summary",
    "impact",
    "confidence",
    "scope",
}
CHECKPOINT_FIELDS = {"id", "updatedAt"}

FRICTION_CATEGORIES = {
    "skill-error",
    "tool-failure",
    "permission-loop",
    "brittle-workflow",
    "missing-context",
    "back-and-forth",
    "other",
}
FRICTION_SEVERITIES = {
    "blocker",
    "non-blocker",
    "dated-doc",
    "deprecated",
    "unclear",
    "observation",
}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
SCOPES = {"task", "project", "skill", "user-wide"}

RAW_OPERATION_PATTERN = re.compile(
    r'"operation"\s*:\s*"(?:read-visible|inventory)"', re.IGNORECASE
)
RAW_BODY_PATTERN = re.compile(
    r'"(?:tasks|messages|turns|items|reasoningAlwaysOmitted)"\s*:', re.IGNORECASE
)
FORBIDDEN_RAW_FIELD_PATTERN = re.compile(
    r'"(?:tasks|messages|turns|items|reasoning|arguments|result|command|diff)"\s*:',
    re.IGNORECASE,
)

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
    re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
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


class ValidationError(RuntimeError):
    """Input is not safe or does not match the storage contract."""


class StorageError(RuntimeError):
    """Local durable storage failed."""

    def __init__(
        self,
        message: str,
        *,
        report_saved: bool = False,
        state_advanced: bool = False,
        partial_receipt: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.report_saved = report_saved
        self.state_advanced = state_advanced
        self.partial_receipt = dict(partial_receipt or {})


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise StorageError("CODEX_HOME must be an absolute local path.")
        try:
            return candidate.resolve(strict=False)
        except OSError as exc:
            raise StorageError("CODEX_HOME could not be resolved safely.") from exc
    return (Path.home() / ".codex").resolve(strict=False)


def default_data_root() -> Path:
    return _default_codex_home() / "daily-reflection"


def _contains_secret(values: Sequence[str]) -> bool:
    return any(pattern.search(value) for value in values for pattern in SECRET_PATTERNS)


def _require_string(
    value: Any,
    field: str,
    *,
    required: bool = True,
    maximum: int = MAX_FIELD_CHARS,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValidationError(f"{field} must be a non-empty string.")
    if "\x00" in value or len(value) > maximum:
        raise ValidationError(f"{field} is invalid or exceeds its size limit.")
    return value.strip() if maximum != MAX_REPORT_CHARS else value


def _parse_iso(value: str, field: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(f"{field} must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a UTC offset.")
    return parsed


def _validate_reflection_date(value: Any) -> str:
    text = _require_string(value, "reflectionDate", maximum=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("reflectionDate must be YYYY-MM-DD.") from exc
    return parsed.isoformat()


def _validate_report(value: Any) -> str:
    report = _require_string(
        value,
        "reportMarkdown",
        maximum=MAX_REPORT_CHARS,
    )
    if "## Scope and omissions" not in report:
        raise ValidationError("reportMarkdown is missing the required scope section.")
    if "## Applied local changes" not in report:
        raise ValidationError("reportMarkdown is missing the required applied-changes section.")
    if FORBIDDEN_RAW_FIELD_PATTERN.search(report) or (
        RAW_OPERATION_PATTERN.search(report) and RAW_BODY_PATTERN.search(report)
    ):
        raise ValidationError("reportMarkdown appears to contain a raw task packet.")
    stripped = report.lstrip()
    if stripped.startswith("{"):
        try:
            candidate = json.loads(stripped)
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, Mapping) and {
            "tasks",
            "turns",
            "items",
            "messages",
            "reasoning",
            "arguments",
            "result",
        }.intersection(candidate):
            raise ValidationError("reportMarkdown must be a synthesized report.")
    if _contains_secret((report,)):
        raise ValidationError("reportMarkdown contains a possible credential.")
    return report.rstrip() + "\n"


def _only_fields(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValidationError(f"{field} contains unknown fields.")


def _validate_friction(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FRICTION_ENTRIES:
        raise ValidationError("friction must be a bounded list.")
    entries: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationError("Each friction entry must be an object.")
        _only_fields(item, FRICTION_FIELDS, f"friction[{index}]")
        category = _require_string(item.get("category"), "friction category", maximum=40)
        severity = _require_string(item.get("severity"), "friction severity", maximum=40)
        confidence = _require_string(item.get("confidence"), "friction confidence", maximum=20)
        scope = _require_string(item.get("scope"), "friction scope", maximum=20)
        if category not in FRICTION_CATEGORIES:
            raise ValidationError("A friction category is unsupported.")
        if severity not in FRICTION_SEVERITIES:
            raise ValidationError("A friction severity is unsupported.")
        if confidence not in CONFIDENCE_LEVELS:
            raise ValidationError("A friction confidence is unsupported.")
        if scope not in SCOPES:
            raise ValidationError("A friction scope is unsupported.")
        entry = {
            "category": category,
            "skill": _require_string(
                item.get("skill"), "friction skill", required=False, maximum=100
            ),
            "severity": severity,
            "summary": _require_string(item.get("summary"), "friction summary", maximum=500),
            "impact": _require_string(
                item.get("impact"), "friction impact", required=False, maximum=500
            ),
            "confidence": confidence,
            "scope": scope,
        }
        if _contains_secret(tuple(entry.values())):
            raise ValidationError("A friction entry contains a possible credential.")
        entries.append(entry)
    return entries


def _validate_checkpoints(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CHECKPOINTS:
        raise ValidationError("taskCheckpoints must be a bounded list.")
    checkpoints: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValidationError("Each task checkpoint must be an object.")
        _only_fields(item, CHECKPOINT_FIELDS, f"taskCheckpoints[{index}]")
        identifier = _require_string(item.get("id"), "task checkpoint ID", maximum=200)
        if identifier in seen:
            raise ValidationError("Task checkpoint IDs must be unique.")
        if _contains_secret((identifier,)):
            raise ValidationError("A task checkpoint ID contains a possible credential.")
        seen.add(identifier)
        updated_at = _require_string(
            item.get("updatedAt"), "task checkpoint timestamp", maximum=50
        )
        parsed = _parse_iso(updated_at, "task checkpoint timestamp")
        checkpoints.append(
            {
                "id": identifier,
                "updatedAt": parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        )
    return checkpoints


def validate_envelope(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValidationError("Input must be a JSON object.")
    _only_fields(payload, TOP_LEVEL_FIELDS, "input")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise ValidationError("schemaVersion is unsupported.")
    timezone_name = _require_string(payload.get("timezone"), "timezone", maximum=100)
    report = _validate_report(payload.get("reportMarkdown"))
    return {
        "reflectionDate": _validate_reflection_date(payload.get("reflectionDate")),
        "timezone": timezone_name,
        "reportMarkdown": report,
        "friction": _validate_friction(payload.get("friction")),
        "taskCheckpoints": _validate_checkpoints(payload.get("taskCheckpoints")),
    }


def _project_identity(project_root: str | None) -> tuple[str, str]:
    if not project_root:
        return "current-task", "Current task"
    expanded = os.path.expandvars(os.path.expanduser(project_root))
    absolute = os.path.abspath(os.path.normpath(expanded))
    normalized = os.path.normcase(absolute)
    key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    label = Path(absolute).name or "Project"
    return key, label[:100]


def _reject_project_data_root(data_root: Path, project_root: str | None) -> None:
    if not project_root:
        return
    try:
        root_resolved = data_root.expanduser().resolve(strict=False)
        project_resolved = Path(
            os.path.abspath(os.path.expandvars(os.path.expanduser(project_root)))
        ).resolve(strict=False)
    except OSError as exc:
        raise StorageError("Could not validate the reflection data boundary.") from exc
    if root_resolved == project_resolved or project_resolved in root_resolved.parents:
        raise StorageError("Reflection data cannot be stored inside the active project.")


REPORT_PATH_PATTERN = re.compile(
    r"^reflections/\d{4}-\d{2}-\d{2}/reflection-[A-Za-z0-9._-]+\.md$"
)
PROJECT_KEY_PATTERN = re.compile(r"^(?:[0-9a-f]{24}|current-task)$")


def _validate_report_path(value: Any, field: str) -> str:
    text = _require_string(value, field, maximum=300)
    if not REPORT_PATH_PATTERN.fullmatch(text) or ".." in text:
        raise StorageError("Existing reflection state contains an unsafe report path.")
    return text


def _validate_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise StorageError("Existing reflection state has an unsupported format.")
    allowed_top = {
        "schemaVersion",
        "generation",
        "lastSuccessfulAt",
        "lastReport",
        "projects",
    }
    if set(payload) - allowed_top or payload.get("schemaVersion") != SCHEMA_VERSION:
        raise StorageError("Existing reflection state has an unsupported format.")
    generation = payload.get("generation")
    projects = payload.get("projects")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or not isinstance(projects, Mapping)
        or len(projects) > 2_000
    ):
        raise StorageError("Existing reflection state has an unsupported format.")
    normalized: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "generation": generation,
        "projects": {},
    }
    if generation == 0 and (projects or "lastSuccessfulAt" in payload or "lastReport" in payload):
        raise StorageError("Existing reflection state has an unsupported format.")
    if generation > 0 and not projects:
        raise StorageError("Existing reflection state has an unsupported format.")
    if generation:
        last_at = _require_string(
            payload.get("lastSuccessfulAt"), "state lastSuccessfulAt", maximum=50
        )
        _parse_iso(last_at, "state lastSuccessfulAt")
        normalized["lastSuccessfulAt"] = last_at
        normalized["lastReport"] = _validate_report_path(
            payload.get("lastReport"), "state lastReport"
        )
    project_allowed = {"lastSuccessfulAt", "lastReport", "taskCheckpoints"}
    for key, value in projects.items():
        if not isinstance(key, str) or not PROJECT_KEY_PATTERN.fullmatch(key):
            raise StorageError("Existing reflection state contains an invalid project key.")
        if not isinstance(value, Mapping) or set(value) - project_allowed:
            raise StorageError("Existing reflection state contains an invalid project record.")
        last_at = _require_string(
            value.get("lastSuccessfulAt"), "project lastSuccessfulAt", maximum=50
        )
        _parse_iso(last_at, "project lastSuccessfulAt")
        report_path = _validate_report_path(value.get("lastReport"), "project lastReport")
        checkpoints = value.get("taskCheckpoints")
        if not isinstance(checkpoints, Mapping) or len(checkpoints) > MAX_CHECKPOINTS:
            raise StorageError("Existing reflection state contains invalid checkpoints.")
        safe_checkpoints: dict[str, str] = {}
        for identifier, updated_at in checkpoints.items():
            if (
                not isinstance(identifier, str)
                or not identifier
                or len(identifier) > 200
                or _contains_secret((identifier,))
                or not isinstance(updated_at, str)
                or len(updated_at) > 50
            ):
                raise StorageError("Existing reflection state contains invalid checkpoints.")
            _parse_iso(updated_at, "checkpoint timestamp")
            safe_checkpoints[identifier] = updated_at
        normalized["projects"][key] = {
            "lastSuccessfulAt": last_at,
            "lastReport": report_path,
            "taskCheckpoints": safe_checkpoints,
        }
    return normalized


def _load_state(path: Path) -> dict[str, Any]:
    if _is_link_or_junction(path):
        raise StorageError("Reflection state cannot be read through a link.")
    if not path.exists():
        return {"schemaVersion": SCHEMA_VERSION, "generation": 0, "projects": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError("Existing reflection state is unreadable.") from exc
    try:
        return _validate_state(payload)
    except ValidationError as exc:
        raise StorageError("Existing reflection state has an unsupported format.") from exc


def _is_link_or_junction(path: Path) -> bool:
    try:
        return path.is_symlink() or (
            hasattr(os.path, "isjunction") and os.path.isjunction(path)
        )
    except OSError:
        return True


def _validate_existing_ancestors(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            if candidate.exists() and _is_link_or_junction(candidate):
                raise StorageError("Reflection storage cannot traverse a link or junction.")
        except OSError as exc:
            raise StorageError("Could not validate the reflection storage path.") from exc


def _validate_data_root_for_read(root: Path) -> None:
    _validate_existing_ancestors(root)
    try:
        if root.exists() and not root.is_dir():
            raise StorageError("The reflection data root is not a directory.")
    except OSError as exc:
        raise StorageError("Could not validate the reflection data root.") from exc


def _ensure_local_directory(path: Path, root: Path) -> None:
    _validate_existing_ancestors(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise StorageError("The reflection data root is not a directory.")
        root_resolved = root.resolve()
    except OSError as exc:
        raise StorageError("Could not create or resolve the reflection data root.") from exc
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise StorageError("The reflection destination escapes its local data root.") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists():
            if _is_link_or_junction(current) or not current.is_dir():
                raise StorageError("A reflection destination component is unsafe.")
        else:
            try:
                current.mkdir()
            except OSError as exc:
                raise StorageError("Could not create the reflection destination.") from exc
        try:
            resolved = current.resolve()
        except OSError as exc:
            raise StorageError("Could not resolve the reflection destination.") from exc
        if root_resolved not in resolved.parents:
            raise StorageError("The reflection destination escapes its local data root.")


def _new_report_path(directory: Path, stamp: str) -> Path:
    for suffix in range(1_000):
        ending = "" if suffix == 0 else f"-{suffix:02d}"
        candidate = directory / f"reflection-{stamp}{ending}.md"
        if not candidate.exists():
            return candidate
    raise StorageError("Could not allocate a non-overwriting report filename.")


@contextmanager
def _transaction_lock(root: Path):
    lock_path = root / ".commit.lock"
    if _is_link_or_junction(lock_path):
        raise StorageError("The reflection transaction lock cannot be a link.")
    try:
        stream = lock_path.open("a+b")
    except OSError as exc:
        raise StorageError("Could not open the reflection transaction lock.") from exc
    acquired = False
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        while not acquired:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise StorageError("Timed out waiting for another reflection commit.")
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        stream.close()


def _write_report(path: Path, report: str) -> tuple[str, int]:
    encoded = report.encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".report-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_name, path)
    except OSError as exc:
        raise StorageError("Could not atomically save the reflection report.") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _append_friction(path: Path, entries: Sequence[Mapping[str, Any]]) -> None:
    if not entries:
        return
    if _is_link_or_junction(path):
        raise StorageError("The friction log cannot be written through a link.")
    block = "".join(
        json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        for entry in entries
    ).encode("utf-8")
    temporary_name: str | None = None
    try:
        if path.exists() and path.stat().st_size > MAX_FRICTION_FILE_BYTES:
            raise StorageError("The friction log exceeds its safe size limit.")
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=path.parent,
            prefix=".friction-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            if path.exists():
                with path.open("rb") as existing:
                    shutil.copyfileobj(existing, stream, length=1024 * 1024)
            stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise StorageError("Could not atomically append the reflection friction log.") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    if _is_link_or_junction(path):
        raise StorageError("Reflection state cannot be written through a link.")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".state-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except OSError as exc:
        raise StorageError("Could not atomically advance reflection state.") from exc
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def commit(
    payload: Any,
    *,
    project_root: str,
    data_root: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    _require_string(project_root, "project root", maximum=2_000)
    envelope = validate_envelope(payload)
    root = data_root or default_data_root()
    state_path = root / "state.json"
    friction_path = root / "friction.jsonl"
    report_dir = root / "reflections" / envelope["reflectionDate"]

    _reject_project_data_root(root, project_root)
    _ensure_local_directory(root, root)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValidationError("The storage timestamp must be timezone-aware.")
    saved_at = instant.astimezone(timezone.utc)
    stamp = saved_at.strftime("%H%M%S.%fZ")
    saved_at_text = saved_at.isoformat().replace("+00:00", "Z")
    project_key, _ = _project_identity(project_root)

    with _transaction_lock(root):
        existing_state = _load_state(state_path)
        _ensure_local_directory(report_dir, root)
        report_path = _new_report_path(report_dir, stamp)
        relative_report = report_path.relative_to(root).as_posix()
        report_suffix = report_path.stem.removeprefix("reflection-")
        report_id = f"{envelope['reflectionDate'].replace('-', '')}T{report_suffix}"
        report_saved = False
        friction_appended = 0
        report_sha = ""
        report_bytes = 0
        try:
            report_sha, report_bytes = _write_report(
                report_path, envelope["reportMarkdown"]
            )
            report_saved = True

            friction_rows = []
            for index, entry in enumerate(envelope["friction"], start=1):
                friction_rows.append(
                    {
                        "schemaVersion": SCHEMA_VERSION,
                        "eventId": f"{report_id}:{index:04d}",
                        "recordedAt": saved_at_text,
                        "reportId": report_id,
                        "reportPath": relative_report,
                        "projectKey": project_key,
                        **entry,
                    }
                )
            _append_friction(friction_path, friction_rows)
            friction_appended = len(friction_rows)

            projects = dict(existing_state.get("projects", {}))
            previous_project = projects.get(project_key, {})
            checkpoint_map = dict(previous_project.get("taskCheckpoints", {}))
            for checkpoint in envelope["taskCheckpoints"]:
                previous = checkpoint_map.get(checkpoint["id"])
                if previous is None or _parse_iso(
                    checkpoint["updatedAt"], "checkpoint timestamp"
                ) > _parse_iso(previous, "existing checkpoint timestamp"):
                    checkpoint_map[checkpoint["id"]] = checkpoint["updatedAt"]
            ordered = sorted(
                checkpoint_map.items(),
                key=lambda item: _parse_iso(item[1], "checkpoint timestamp"),
                reverse=True,
            )
            checkpoint_map = dict(ordered[:MAX_CHECKPOINTS])
            project_state = {
                "lastSuccessfulAt": saved_at_text,
                "lastReport": relative_report,
                "taskCheckpoints": checkpoint_map,
            }
            projects[project_key] = project_state
            next_state = {
                "schemaVersion": SCHEMA_VERSION,
                "generation": existing_state["generation"] + 1,
                "lastSuccessfulAt": saved_at_text,
                "lastReport": relative_report,
                "projects": projects,
            }
            _validate_state(next_state)
            _atomic_write_json(state_path, next_state)
        except (StorageError, ValidationError) as exc:
            partial_receipt: dict[str, Any] = {}
            if report_saved:
                partial_receipt["report"] = {
                    "id": report_id,
                    "path": relative_report,
                    "savedAt": saved_at_text,
                    "sha256": report_sha,
                    "bytes": report_bytes,
                }
            partial_receipt["frictionAppended"] = friction_appended
            raise StorageError(
                str(exc),
                report_saved=report_saved,
                state_advanced=False,
                partial_receipt=partial_receipt,
            ) from exc

    return {
        "schemaVersion": SCHEMA_VERSION,
        "operation": "commit",
        "ok": True,
        "report": {
            "id": report_id,
            "path": relative_report,
            "savedAt": saved_at_text,
            "sha256": report_sha,
            "bytes": report_bytes,
        },
        "frictionAppended": friction_appended,
        "state": {
            "path": "state.json",
            "generation": next_state["generation"],
            "advanced": True,
            "projectKey": project_key,
        },
    }


def status(*, project_root: str, data_root: Path | None = None) -> dict[str, Any]:
    _require_string(project_root, "project root", maximum=2_000)
    root = data_root or default_data_root()
    _reject_project_data_root(root, project_root)
    _validate_data_root_for_read(root)
    state = _load_state(root / "state.json")
    project_key, _ = _project_identity(project_root)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "operation": "status",
        "generation": state["generation"],
        "lastSuccessfulAt": state.get("lastSuccessfulAt"),
        "lastReport": state.get("lastReport"),
        "projectKey": project_key,
        "project": state.get("projects", {}).get(project_key),
    }


def _bounded_friction_lines(path: Path) -> tuple[list[str], bool]:
    if _is_link_or_junction(path):
        raise StorageError("The friction log cannot be read through a link.")
    if not path.exists():
        return [], False
    try:
        size = path.stat().st_size
        start = max(0, size - MAX_FRICTION_READ_BYTES)
        with path.open("rb") as stream:
            stream.seek(start)
            block = stream.read(MAX_FRICTION_READ_BYTES + 1)
    except OSError as exc:
        raise StorageError("Could not read recent reflection friction.") from exc
    partial = start > 0 or len(block) > MAX_FRICTION_READ_BYTES
    block = block[:MAX_FRICTION_READ_BYTES]
    if start > 0:
        first_newline = block.find(b"\n")
        block = b"" if first_newline < 0 else block[first_newline + 1 :]
    try:
        text = block.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageError("Recent reflection friction is not valid UTF-8.") from exc
    return text.splitlines(), partial


def recent_friction(
    *,
    project_root: str,
    since: str,
    limit: int = 100,
    data_root: Path | None = None,
) -> dict[str, Any]:
    _require_string(project_root, "project root", maximum=2_000)
    if limit <= 0 or limit > MAX_RECENT_FRICTION:
        raise ValidationError(f"limit must be between 1 and {MAX_RECENT_FRICTION}.")
    since_at = _parse_iso(since, "since").astimezone(timezone.utc)
    root = data_root or default_data_root()
    _reject_project_data_root(root, project_root)
    _validate_data_root_for_read(root)
    project_key, _ = _project_identity(project_root)
    lines, partial = _bounded_friction_lines(root / "friction.jsonl")
    entries: list[dict[str, Any]] = []
    malformed_or_unsafe = 0
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            malformed_or_unsafe += 1
            continue
        if (
            not isinstance(item, Mapping)
            or item.get("schemaVersion") != SCHEMA_VERSION
            or item.get("projectKey") != project_key
        ):
            continue
        try:
            recorded_at = _require_string(
                item.get("recordedAt"), "recordedAt", maximum=50
            )
            parsed = _parse_iso(recorded_at, "recordedAt").astimezone(timezone.utc)
            report_id = _require_string(item.get("reportId"), "reportId", maximum=200)
            report_path = _validate_report_path(item.get("reportPath"), "reportPath")
            friction_source = {key: item.get(key) for key in FRICTION_FIELDS}
            validated_friction = _validate_friction([friction_source])[0]
        except (ValidationError, StorageError):
            malformed_or_unsafe += 1
            continue
        if parsed < since_at:
            continue
        safe = {
            "recordedAt": recorded_at,
            "reportId": report_id,
            "reportPath": report_path,
            **validated_friction,
        }
        serialized_values = tuple(str(value) for value in safe.values())
        if _contains_secret(serialized_values):
            malformed_or_unsafe += 1
            continue
        entries.append(safe)
    entries.sort(key=lambda item: str(item["recordedAt"]), reverse=True)
    if len(entries) > limit:
        entries = entries[:limit]
        partial = True
    return {
        "schemaVersion": SCHEMA_VERSION,
        "operation": "recent-friction",
        "projectKey": project_key,
        "since": since_at.isoformat().replace("+00:00", "Z"),
        "entries": entries,
        "omissions": {
            "partial": partial,
            "malformedOrUnsafeEntries": malformed_or_unsafe,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Store a sanitized daily reflection locally.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    commit_parser = subparsers.add_parser("commit")
    commit_parser.add_argument("--project-root", required=True)
    commit_parser.add_argument(
        "--input",
        type=Path,
        help="Read the strict JSON envelope from this local file instead of stdin.",
    )
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--project-root", required=True)
    recent_parser = subparsers.add_parser("recent-friction")
    recent_parser.add_argument("--project-root", required=True)
    recent_parser.add_argument("--since", required=True)
    recent_parser.add_argument("--limit", type=int, default=100)
    return parser


def _read_stdin_json() -> Any:
    raw = sys.stdin.read(MAX_INPUT_CHARS + 1)
    if len(raw) > MAX_INPUT_CHARS:
        raise ValidationError("Input exceeds the storage size limit.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("Input must be valid JSON.") from exc


def _read_input_json(path: Path | None) -> Any:
    if path is None:
        return _read_stdin_json()
    if _is_link_or_junction(path):
        raise ValidationError("The input envelope cannot be a link.")
    try:
        if path.stat().st_size > MAX_INPUT_CHARS:
            raise ValidationError("Input exceeds the storage size limit.")
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValidationError("The input envelope could not be read safely.") from exc
    if len(raw) > MAX_INPUT_CHARS:
        raise ValidationError("Input exceeds the storage size limit.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError("Input must be valid JSON.") from exc


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            result = status(project_root=args.project_root)
        elif args.command == "recent-friction":
            result = recent_friction(
                project_root=args.project_root,
                since=args.since,
                limit=args.limit,
            )
        else:
            result = commit(
                _read_input_json(args.input), project_root=args.project_root
            )
    except ValidationError as exc:
        print(
            json.dumps({"ok": False, "error": {"type": "validation", "message": str(exc)}}),
            file=sys.stderr,
        )
        return 2
    except StorageError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {"type": "storage", "message": str(exc)},
                    "reportSaved": exc.report_saved,
                    "stateAdvanced": exc.state_advanced,
                    "partialReceipt": exc.partial_receipt,
                }
            ),
            file=sys.stderr,
        )
        return 3
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
