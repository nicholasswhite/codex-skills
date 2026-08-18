#!/usr/bin/env python3
"""Strict handoff validator and deterministic continuation-prompt renderer.

Subcommands:
  check  --file HANDOFF.md [--branch X --default Y] [--strict] [--json]
  prompt --spec SPEC.json [--out FILE]

The linter enforces structure and reports stale-state risks. `--strict` makes
warnings fail, which is the required mode for the session-handoff skill.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


_TIP_KEYWORDS = (
    r"(?:current\s+(?:tip|head|sha|commit)|currently\s+at|now\s+at|tip|head)"
)
_HASH = r"[0-9a-f]{7,64}"
_CONNECTOR = r"(?:\s+(?:is|at|now|=|->|:|points\s+to))*"
_SEPARATOR = r"[\s:=`>()\-]*"
_PIN_RE = re.compile(
    rf"(?i)\b{_TIP_KEYWORDS}\b{_CONNECTOR}{_SEPARATOR}\b({_HASH})\b"
)

_DEFAULT_REQUIRED = ("read", "current state", "last session", "constraint", "next")
_REVERIFY_RE = re.compile(
    r"(?i)\b(?:re-?derive|re-?run|re-?confirm|re-?verify|self-invalidat\w*)\b"
    r"|\b(?:do not|don't) trust\b"
    r"|\b(?:verify|confirm)\b[^\n.]{0,80}\b(?:before|again|current|live)\b"
)
_PUSH_GUARD_RE = re.compile(
    r"(?i)\b(?:do not|don't|never)\b[^\n.]{0,100}\bpush\b"
    r"|\bpush\b[^\n.]{0,100}\b(?:only|unless)\b[^\n.]{0,100}"
    r"\b(?:explicit|authoriz|request|permission|permit)\w*\b"
    r"|\b(?:keep|remain)\b[^\n.]{0,30}\b(?:local|unpublished)\b"
)
_COMMIT_GUARD_RE = re.compile(
    r"(?i)\b(?:do not|don't|never)\b[^\n.]{0,100}\b(?:stage|commit)\b"
    r"|\b(?:stage|commit)\b[^\n.]{0,100}\b(?:only|unless)\b[^\n.]{0,100}"
    r"\b(?:explicit|authoriz|request|permission|permit)\w*\b"
)


def find_pinned_tips(text: str) -> list[str]:
    """Return hashes asserted as the current tip/HEAD/SHA in prose."""
    pins: list[str] = []
    for match in _PIN_RE.finditer(text):
        value = match.group(1).lower()
        if not value.isdigit():
            pins.append(value)
    return pins


def _heading_lines(text: str) -> list[str]:
    headings: list[str] = []
    fence: Optional[str] = None
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in ("```", "~~~"):
            fence = None if fence == marker else marker if fence is None else fence
            continue
        if fence is None and re.match(r"^#{1,6}(?:\s+|$)", stripped):
            headings.append(stripped.lstrip("#").strip().lower())
    return headings


def check_required_sections(
    text: str, required: tuple[str, ...] = _DEFAULT_REQUIRED
) -> list[str]:
    headings = _heading_lines(text)
    return [need for need in required if not any(need in h for h in headings)]


def check_reverify(text: str) -> bool:
    return _REVERIFY_RE.search(text) is not None


def check_push_guard(
    text: str, branch: Optional[str] = None, default_branch: Optional[str] = None
) -> Optional[str]:
    if not branch or not default_branch or branch == default_branch:
        return None
    if _PUSH_GUARD_RE.search(text):
        return None
    return (
        f"working branch '{branch}' differs from default '{default_branch}' "
        "but no push guard is recorded"
    )


def check_mutation_guards(text: str) -> list[str]:
    """Require explicit commit and push boundaries in every handoff."""
    warnings: list[str] = []
    if not _COMMIT_GUARD_RE.search(text):
        warnings.append(
            "no commit-authorization guard found (state that commits require "
            "explicit user authorization and project permission)"
        )
    if not _PUSH_GUARD_RE.search(text):
        warnings.append("no push guard found (state that pushing is not authorized)")
    return warnings


@dataclass
class LintReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)

    def ok(self, *, strict: bool = False) -> bool:
        return not self.errors and (not strict or not self.warnings)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok(),
            "strict_ok": self.ok(strict=True),
            "errors": self.errors,
            "warnings": self.warnings,
            "infos": self.infos,
        }


def lint(
    text: str,
    *,
    branch: Optional[str] = None,
    default_branch: Optional[str] = None,
    required_sections: Optional[tuple[str, ...]] = None,
) -> LintReport:
    report = LintReport()
    if not text.strip():
        report.errors.append("empty handoff document")
        return report

    required = required_sections or _DEFAULT_REQUIRED
    for missing in check_required_sections(text, required):
        report.errors.append(f"missing required section: '{missing}'")

    for value in find_pinned_tips(text):
        report.warnings.append(
            "pinned current tip hash in prose (stale-prone; describe how to "
            f"re-derive it instead): {value}"
        )

    if not check_reverify(text):
        report.warnings.append(
            "no verify/re-derive instruction found; the next session may trust "
            "stale state"
        )

    report.warnings.extend(check_mutation_guards(text))
    push_warning = check_push_guard(text, branch, default_branch)
    if push_warning and push_warning not in report.warnings:
        report.warnings.append(push_warning)
    return report


def render_threads_menu(threads: list) -> str:
    lines: list[str] = []
    for index, thread in enumerate(threads):
        label = chr(ord("a") + index) if index < 26 else f"a{index}"
        if isinstance(thread, dict):
            title = str(thread.get("title", "")).strip()
            desc = str(thread.get("desc", "")).strip()
            line = f"- ({label}) **{title}**"
            if desc:
                line += f" — {desc}"
        else:
            line = f"- ({label}) {str(thread).strip()}"
        lines.append(line)
    return "\n".join(lines)


def _render_read_order(read_order: list) -> str:
    lines: list[str] = []
    for index, entry in enumerate(read_order, 1):
        if isinstance(entry, dict):
            path = str(entry.get("path", "")).strip()
            note = str(entry.get("note", "")).strip()
            line = f"{index}. `{path}`"
            if note:
                line += f" — {note}"
        else:
            line = f"{index}. {str(entry).strip()}"
        lines.append(line)
    return "\n".join(lines)


def _bullets(items: list) -> str:
    return "\n".join(f"- {str(item).strip()}" for item in items)


def validate_prompt_spec(spec: object) -> list[str]:
    if not isinstance(spec, dict):
        return ["prompt specification must be a JSON object"]

    errors: list[str] = []
    if not str(spec.get("project", "")).strip():
        errors.append("prompt specification requires a non-empty 'project'")

    for key in (
        "read_order",
        "state_lines",
        "accomplishments",
        "constraints",
        "threads",
    ):
        value = spec.get(key)
        if not isinstance(value, list) or not value:
            errors.append(f"prompt specification requires a non-empty '{key}' list")

    for key in ("state_lines", "accomplishments", "constraints"):
        value = spec.get(key)
        if isinstance(value, list):
            for index, entry in enumerate(value):
                if not isinstance(entry, str) or not entry.strip():
                    errors.append(f"{key}[{index}] must be a non-empty string")

    for key in ("verify_note", "ask"):
        if key in spec and (
            not isinstance(spec[key], str) or not spec[key].strip()
        ):
            errors.append(f"'{key}' must be a non-empty string when supplied")

    constraints = spec.get("constraints")
    if isinstance(constraints, list):
        errors.extend(check_mutation_guards("\n".join(str(x) for x in constraints)))

    for index, entry in enumerate(spec.get("read_order", [])):
        if isinstance(entry, dict):
            if not str(entry.get("path", "")).strip():
                errors.append(f"read_order[{index}] requires a non-empty path")
        elif not isinstance(entry, str) or not entry.strip():
            errors.append(f"read_order[{index}] must be a non-empty string or object")

    for index, entry in enumerate(spec.get("threads", [])):
        if isinstance(entry, dict):
            if not str(entry.get("title", "")).strip():
                errors.append(f"threads[{index}] requires a non-empty title")
        elif not isinstance(entry, str) or not entry.strip():
            errors.append(f"threads[{index}] must be a non-empty string or object")
    return errors


def render_pickup_prompt(
    *,
    project: str,
    read_order: list,
    state_lines: list,
    accomplishments: list,
    constraints: list,
    threads: list,
    verify_note: Optional[str] = None,
    ask: Optional[str] = None,
) -> str:
    verify = (
        "Reread applicable project instructions and durable docs, inspect live "
        "artifacts, and rerun checks before acting. If Git exists, re-derive "
        "its branch, tip, comparison, and working-tree state; treat all "
        "last-known numbers below as stale until verified."
    )
    if verify_note:
        verify += " " + verify_note.strip()
    closing = ask or (
        "Choose one open thread (or propose another). Start by re-deriving the "
        "live state, then confirm the plan before changing anything."
    )
    return "\n".join(
        [
            f"# Continue: {project}",
            "",
            "You're picking up an in-progress project. **Verify before you trust.**",
            "",
            "## Read first (in order)",
            _render_read_order(read_order),
            "",
            "## Where things stand",
            f"> {verify}",
            "",
            _bullets(state_lines),
            "",
            "## What the last session did",
            _bullets(accomplishments),
            "",
            "## Hard constraints (do not violate)",
            _bullets(constraints),
            "",
            "## Open threads — pick one",
            render_threads_menu(threads),
            "",
            closing,
            "",
        ]
    )


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    report = lint(text, branch=args.branch, default_branch=args.default)
    if args.json:
        output = report.to_dict()
        output["selected_ok"] = report.ok(strict=args.strict)
        print(json.dumps(output, indent=2))
    else:
        for error in report.errors:
            print(f"  ERROR    {error}")
        for warning in report.warnings:
            print(f"  WARN     {warning}")
        for info in report.infos:
            print(f"  INFO     {info}")
        verdict = "OK" if report.ok(strict=args.strict) else "FAILED"
        mode = "strict" if args.strict else "standard"
        print(
            f"\n{verdict} ({mode}): {len(report.errors)} error(s), "
            f"{len(report.warnings)} warning(s)"
        )
    return 0 if report.ok(strict=args.strict) else 1


def _cmd_prompt(args: argparse.Namespace) -> int:
    try:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: cannot read {args.spec}: {exc}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in {args.spec}: {exc}", file=sys.stderr)
        return 2

    spec_errors = validate_prompt_spec(spec)
    if spec_errors:
        for error in spec_errors:
            print(f"error: {error}", file=sys.stderr)
        return 2

    output = render_pickup_prompt(
        project=str(spec["project"]).strip(),
        read_order=spec["read_order"],
        state_lines=spec["state_lines"],
        accomplishments=spec["accomplishments"],
        constraints=spec["constraints"],
        threads=spec["threads"],
        verify_note=spec.get("verify_note"),
        ask=spec.get("ask"),
    )
    pinned = find_pinned_tips(output)
    if pinned:
        print(
            "error: rendered prompt pins current tip hash(es): " + ", ".join(pinned),
            file=sys.stderr,
        )
        return 2

    rendered_report = lint(
        output,
        required_sections=(
            "read first",
            "where things stand",
            "last session",
            "hard constraints",
            "open threads",
        ),
    )
    if not rendered_report.ok(strict=True):
        for error in rendered_report.errors:
            print(f"error: rendered prompt: {error}", file=sys.stderr)
        for warning in rendered_report.warnings:
            print(f"error: rendered prompt: {warning}", file=sys.stderr)
        return 2

    if args.out:
        try:
            Path(args.out).write_text(output, encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
            return 2
        print(f"wrote {args.out}")
    else:
        print(output)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate handoff documents and render continuation prompts."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser("check", help="lint a HANDOFF.md")
    check_parser.add_argument("--file", required=True)
    check_parser.add_argument("--branch", default=None)
    check_parser.add_argument("--default", default=None)
    check_parser.add_argument("--strict", action="store_true")
    check_parser.add_argument("--json", action="store_true")
    check_parser.set_defaults(handler=_cmd_check)

    prompt_parser = sub.add_parser("prompt", help="render a prompt from JSON")
    prompt_parser.add_argument("--spec", required=True)
    prompt_parser.add_argument("--out", default=None)
    prompt_parser.set_defaults(handler=_cmd_prompt)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
