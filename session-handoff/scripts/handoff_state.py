#!/usr/bin/env python3
"""Capture read-only, self-invalidating project facts for a session handoff.

The command detects whether a path belongs to a Git worktree. It never creates
or mutates a repository. JSON output may include the live tip for programmatic
inspection; Markdown output intentionally omits it so durable handoffs do not
pin stale state.

Subcommands:
  capture --path PATH [--default BRANCH] [--since REF] [--json|--markdown]
  commits --path PATH [--default BRANCH] [--since REF] [--json]

`--repo` is retained as an alias for `--path` for compatibility with earlier
versions of this skill.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


def parse_porcelain(text: str) -> dict:
    """Parse `git status --porcelain` into stable dirty-file facts."""
    entries = [line for line in text.splitlines() if line.strip()]
    files = [line[3:] if len(line) > 3 else line.strip() for line in entries]
    return {"dirty": bool(entries), "changed": len(entries), "files": files}


def parse_log_oneline(text: str) -> list[dict]:
    """Parse `git log --oneline` while preserving order."""
    commits: list[dict] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.split(" ", 1)
        commits.append(
            {"hash": parts[0], "subject": parts[1] if len(parts) > 1 else ""}
        )
    return commits


def parse_count(text: str) -> Optional[int]:
    """Parse a single integer, returning None for invalid command output."""
    value = text.strip()
    return int(value) if value.lstrip("-").isdigit() else None


def _git(path: Path, *args: str) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return 127, "", "git not found on PATH"


def _existing_directory(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"path is not a directory: {path}")
    return path


def discover_git_root(path: Path) -> Optional[Path]:
    """Return the containing Git worktree root, or None without mutating it."""
    rc, out, _ = _git(path, "rev-parse", "--show-toplevel")
    if rc != 0 or not out.strip():
        return None
    return Path(out.strip()).resolve()


def _ref_exists(repo: Path, ref: str) -> bool:
    rc, _, _ = _git(repo, "rev-parse", "--verify", "--quiet", ref)
    return rc == 0


def discover_default_branch(repo: Path) -> Optional[str]:
    """Discover a comparison branch without assuming `main`."""
    rc, out, _ = _git(
        repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"
    )
    if rc == 0 and out.strip().startswith("origin/"):
        return out.strip().split("/", 1)[1]

    for candidate in ("main", "master"):
        if _ref_exists(repo, candidate) or _ref_exists(repo, f"origin/{candidate}"):
            return candidate
    return None


def capture(
    path_value: str,
    default_branch: Optional[str] = None,
    since: Optional[str] = None,
) -> dict:
    """Gather a read-only state snapshot for a Git or non-Git workspace."""
    path = _existing_directory(path_value)
    repo = discover_git_root(path)
    if repo is None:
        return {
            "mode": "filesystem",
            "workspace": str(path),
            "git_available": shutil.which("git") is not None,
            "branch": None,
            "tip": None,
            "default_branch": None,
            "ahead": None,
            "dirty": None,
            "changed": None,
            "changed_files": [],
            "since_ref": None,
            "commits_since": [],
            "commit_count_since": 0,
        }

    rc, out, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    branch = out.strip() if rc == 0 else None
    if branch == "HEAD":
        branch = "(detached)"

    rc, out, _ = _git(repo, "rev-parse", "--short", "HEAD")
    tip = out.strip() if rc == 0 else None

    if default_branch:
        if not (
            _ref_exists(repo, default_branch)
            or _ref_exists(repo, f"origin/{default_branch}")
        ):
            raise ValueError(f"default branch/ref does not exist: {default_branch}")
        default = default_branch
    else:
        default = discover_default_branch(repo)

    compare_ref: Optional[str] = None
    if default:
        compare_ref = default if _ref_exists(repo, default) else f"origin/{default}"

    ahead: Optional[int] = None
    if compare_ref and tip:
        rc, out, _ = _git(repo, "rev-list", "--count", f"{compare_ref}..HEAD")
        ahead = parse_count(out) if rc == 0 else None

    rc, out, _ = _git(repo, "status", "--porcelain")
    if rc == 0:
        status = parse_porcelain(out)
        dirty = status["dirty"]
        changed = status["changed"]
        changed_files = status["files"]
    else:
        dirty = None
        changed = None
        changed_files = []

    selected_ref = since or compare_ref
    if since and not _ref_exists(repo, since):
        raise ValueError(f"session-start ref does not exist: {since}")

    commits: list[dict] = []
    if selected_ref and tip:
        rc, out, _ = _git(
            repo, "log", "--oneline", "--no-decorate", f"{selected_ref}..HEAD"
        )
        if rc != 0:
            raise ValueError(f"could not compare commits from ref: {selected_ref}")
        commits = parse_log_oneline(out)

    return {
        "mode": "git",
        "workspace": str(path),
        "repo_root": str(repo),
        "git_available": True,
        "branch": branch,
        "tip": tip,
        "default_branch": default,
        "comparison_ref": compare_ref,
        "ahead": ahead,
        "dirty": dirty,
        "changed": changed,
        "changed_files": changed_files,
        "since_ref": selected_ref,
        "commits_since": commits,
        "commit_count_since": len(commits),
    }


def render_state_markdown(facts: dict) -> str:
    """Render a durable state block that never includes the current tip hash."""
    if facts.get("mode") != "git":
        git_note = (
            "Git is installed, but this path is not a Git worktree"
            if facts.get("git_available")
            else "Git is unavailable and this path has no detected Git worktree"
        )
        return "\n".join(
            [
                f"- **Workspace:** non-Git mode ({git_note}); branch, tip, and "
                "commit history are unavailable.",
                "- **Verify live state:** reread applicable `AGENTS.md`, inspect "
                "the cited artifacts, and rerun the project checks. Treat every "
                "last-known result as stale until reverified.",
            ]
        )

    branch = facts.get("branch") or "(unknown)"
    default = facts.get("default_branch")
    ahead = facts.get("ahead")
    if default and ahead is not None:
        comparison = f"{ahead} commit(s) ahead of `{default}`"
    elif default:
        comparison = f"comparison with `{default}` available; ahead count unknown"
    else:
        comparison = "default branch not discovered; comparison count unavailable"

    if facts.get("dirty") is False:
        tree = "working tree **clean**"
    elif facts.get("dirty") is True:
        tree = f"**{facts.get('changed', 0)} uncommitted change(s)**"
    else:
        tree = "working-tree status unknown"

    lines = [
        f"- **Git:** branch `{branch}`, {comparison}, {tree}.",
        "- **Verify live state:** run `git status --short --branch`, re-derive "
        "the appropriate default-branch comparison, and rerun the project "
        "checks. Do not trust this last-known state blindly.",
    ]
    commits = facts.get("commits_since") or []
    if commits:
        lines.append(
            f"- **{len(commits)} commit(s) since `{facts.get('since_ref')}`**; "
            "confirm that this ref actually represents the session before using "
            "the list as accomplishments."
        )
    return "\n".join(lines)


def _cmd_capture(args: argparse.Namespace) -> int:
    try:
        facts = capture(args.path, args.default, args.since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render_state_markdown(facts) if args.markdown else json.dumps(facts, indent=2))
    return 0


def _cmd_commits(args: argparse.Namespace) -> int:
    try:
        facts = capture(args.path, args.default, args.since)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if facts["mode"] != "git":
        if args.json:
            print("[]")
        else:
            print(
                "Non-Git workspace: no commit history is available. Ground "
                "accomplishments in verified files and command output."
            )
        return 0

    commits = facts["commits_since"]
    if args.json:
        print(json.dumps(commits, indent=2))
    else:
        for commit in commits:
            print(f"  {commit['hash']}  {commit['subject']}")
        ref = facts.get("since_ref") or "(no comparison ref discovered)"
        print(f"\n{len(commits)} commit(s) since {ref}")
    return 0


def _add_path_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--path", "--repo", dest="path", required=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture read-only Git or filesystem facts for a session handoff."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture_parser = sub.add_parser("capture", help="capture current workspace state")
    _add_path_argument(capture_parser)
    capture_parser.add_argument("--default", default=None)
    capture_parser.add_argument("--since", default=None)
    output = capture_parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--markdown", action="store_true")
    capture_parser.set_defaults(handler=_cmd_capture)

    commits_parser = sub.add_parser("commits", help="list commits from a verified ref")
    _add_path_argument(commits_parser)
    commits_parser.add_argument("--default", default=None)
    commits_parser.add_argument("--since", default=None)
    commits_parser.add_argument("--json", action="store_true")
    commits_parser.set_defaults(handler=_cmd_commits)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
