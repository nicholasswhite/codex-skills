#!/usr/bin/env python3
"""Build, verify, and unpack flat GitHub Gist capsules for nested Codex skills."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path, PurePosixPath


SCHEMA = "codex-skill-gist/v1"
MAX_PAYLOAD_BYTES = 700_000
MAX_MANIFEST_BYTES = 2_000_000
MAX_FILES = 1_000
MAX_FILE_BYTES = 10_000_000
MAX_TOTAL_BYTES = 100_000_000
MAX_SOURCES_PER_FILE = 32
EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
RESERVED_CAPSULE_FILES = {"00-README.md", "CAPSULE.v1.json", "UNPACK.py"}
WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CON",
    "CONIN$",
    "CONOUT$",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    "COM¹",
    "COM²",
    "COM³",
    "LPT¹",
    "LPT²",
    "LPT³",
}
WINDOWS_FORBIDDEN_CHARS = set('<>:"\\|?*')


class CapsuleError(ValueError):
    """Raised when a capsule is malformed or unsafe."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_link(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or is_junction()


def _windows_name_is_reserved(name: str) -> bool:
    if name.endswith((".", " ")) or any(ord(char) < 32 for char in name):
        return True
    if any(char in WINDOWS_FORBIDDEN_CHARS for char in name):
        return True
    checker = getattr(ntpath, "isreserved", None)
    if checker is not None and checker(name):
        return True
    return name.partition(".")[0].rstrip(" ").upper() in WINDOWS_RESERVED_NAMES


def _safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CapsuleError("manifest paths must be non-empty POSIX relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CapsuleError(f"unsafe manifest path: {value!r}")
    for part in path.parts:
        if _windows_name_is_reserved(part):
            raise CapsuleError(f"Windows-reserved manifest path: {value!r}")
    return path.as_posix()


def _excluded(relative: Path) -> bool:
    parts = relative.parts
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    if relative.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    name = relative.name
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return True
    if "outputs" in parts:
        output_index = parts.index("outputs")
        if len(parts) > output_index + 1 and name != ".gitkeep":
            return True
    return False


def source_files(source: Path) -> list[tuple[str, bytes]]:
    if _is_link(source):
        raise CapsuleError("source skill root must not be a link or junction")
    source = source.resolve(strict=True)
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise CapsuleError("source must be a skill directory containing SKILL.md")
    files: list[tuple[str, bytes]] = []
    for current, dirs, names in os.walk(source, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in sorted(dirs):
            candidate = current_path / name
            relative = candidate.relative_to(source)
            if _is_link(candidate):
                raise CapsuleError(f"source contains a linked directory: {relative}")
            if not _excluded(relative):
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(names):
            candidate = current_path / name
            relative = candidate.relative_to(source)
            if _excluded(relative):
                continue
            if _is_link(candidate) or not candidate.is_file():
                raise CapsuleError(f"source contains an unsupported file: {relative}")
            files.append((relative.as_posix(), candidate.read_bytes()))
    files.sort(key=lambda item: item[0])
    _validate_source_limits(files)
    return files


def _validate_source_limits(files: list[tuple[str, bytes]]) -> None:
    if len(files) > MAX_FILES:
        raise CapsuleError(f"skill contains more than {MAX_FILES} distributable files")
    total = 0
    for relative, data in files:
        _safe_relative_path(relative)
        if len(data) > MAX_FILE_BYTES:
            raise CapsuleError(f"source file exceeds the {MAX_FILE_BYTES}-byte limit: {relative}")
        total += len(data)
    if total > MAX_TOTAL_BYTES:
        raise CapsuleError(f"skill exceeds the {MAX_TOTAL_BYTES}-byte capsule limit")


def _git_context(source: Path) -> tuple[Path, str] | None:
    result = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "--show-toplevel", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        raise CapsuleError("could not resolve the source Git repository and HEAD")
    return Path(lines[0]).resolve(strict=True), lines[1]


def _git_files_at_ref(source: Path, repository: Path, source_ref: str) -> list[tuple[str, bytes]]:
    relative_root = source.relative_to(repository).as_posix()
    listing = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", "-r", "-z", "--name-only", source_ref, "--", relative_root],
        capture_output=True,
        check=False,
    )
    if listing.returncode != 0:
        raise CapsuleError("could not list the skill at the requested source ref")
    repo_paths = [item.decode("utf-8") for item in listing.stdout.split(b"\0") if item]
    files: list[tuple[str, bytes]] = []
    for repo_path in repo_paths:
        prefix = relative_root + "/"
        if not repo_path.startswith(prefix):
            raise CapsuleError("Git returned a path outside the source skill")
        relative = _safe_relative_path(repo_path[len(prefix) :])
        if _excluded(Path(*PurePosixPath(relative).parts)):
            raise CapsuleError(f"an excluded generated file is tracked: {relative}")
        blob = subprocess.run(
            ["git", "-C", str(repository), "show", f"{source_ref}:{repo_path}"],
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            raise CapsuleError(f"could not read tracked source file: {relative}")
        files.append((relative, blob.stdout))
    files.sort(key=lambda item: item[0])
    _validate_source_limits(files)
    if not any(path == "SKILL.md" for path, _ in files):
        raise CapsuleError("tracked source ref does not contain SKILL.md")
    return files


def tree_digest(files: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for path, data in sorted(files, key=lambda item: item[0]):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(data).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _payloads(data: bytes, counter: list[int]) -> tuple[str, list[tuple[str, bytes]]]:
    try:
        data.decode("utf-8")
        is_text = len(data) <= MAX_PAYLOAD_BYTES
    except UnicodeDecodeError:
        is_text = False

    counter[0] += 1
    if is_text:
        return "utf-8", [(f"blob-{counter[0]:06d}.txt", data)]

    encoded = base64.b64encode(data)
    chunk_size = MAX_PAYLOAD_BYTES - (MAX_PAYLOAD_BYTES % 4)
    chunks: list[tuple[str, bytes]] = []
    for offset in range(0, len(encoded), chunk_size):
        if chunks:
            counter[0] += 1
        chunks.append((f"blob-{counter[0]:06d}.b64", encoded[offset : offset + chunk_size]))
    return "base64", chunks


def build_capsule(
    source: Path,
    output: Path,
    *,
    source_repo: str,
    source_ref: str,
) -> dict:
    if _is_link(source):
        raise CapsuleError("source skill root must not be a link or junction")
    source = source.resolve(strict=True)
    output = output.resolve(strict=False)
    if output == source or output.is_relative_to(source):
        raise CapsuleError("capsule output must be outside the source skill")
    if output.exists():
        raise CapsuleError("capsule output already exists")
    if not source_repo.startswith("https://github.com/") or not source_ref:
        raise CapsuleError("source repository URL and immutable source ref are required")

    git_context = _git_context(source)
    if git_context:
        repository, head = git_context
        if source_ref != head:
            raise CapsuleError("source ref must equal the current full HEAD commit")
        for arguments in (
            ["diff", "--quiet", "--", str(source.relative_to(repository))],
            ["diff", "--cached", "--quiet", "--", str(source.relative_to(repository))],
        ):
            clean = subprocess.run(["git", "-C", str(repository), *arguments], check=False)
            if clean.returncode != 0:
                raise CapsuleError("source skill has uncommitted changes")
        files = _git_files_at_ref(source, repository, source_ref)
    else:
        files = source_files(source)
    output.mkdir(parents=True)
    counter = [0]
    manifest_files: list[dict] = []
    for relative, data in files:
        if relative == "SKILL.md":
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CapsuleError("SKILL.md must be UTF-8") from exc
            payload_files = [("SKILL.md", data)]
            encoding = "utf-8"
        else:
            encoding, payload_files = _payloads(data, counter)

        for payload_name, payload_data in payload_files:
            if payload_name in RESERVED_CAPSULE_FILES and payload_name != "SKILL.md":
                raise CapsuleError(f"reserved payload name: {payload_name}")
            (output / payload_name).write_bytes(payload_data)

        manifest_files.append(
            {
                "path": relative,
                "sources": [name for name, _ in payload_files],
                "encoding": encoding,
                "size": len(data),
                "sha256": _sha256(data),
            }
        )

    manifest = {
        "schema": SCHEMA,
        "name": source.name,
        "sourceRepository": source_repo,
        "sourceRef": source_ref,
        "sourcePath": source.name,
        "treeSha256": tree_digest(files),
        "files": manifest_files,
    }
    (output / "CAPSULE.v1.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    about = (
        f"# {source.name}\n\n"
        "This public Gist contains a self-contained capsule of a Codex skill. "
        "`SKILL.md` is included directly for reading. The complete nested skill "
        "can be reconstructed from `CAPSULE.v1.json` and the numbered payloads.\n\n"
        f"Canonical source: {source_repo}/tree/{source_ref}/{source.name}\n\n"
        "Install normally from the canonical repository, or clone this Gist and run:\n\n"
        "```text\npython UNPACK.py unpack --capsule-dir . --destination ./skill\n```\n"
    )
    (output / "00-README.md").write_text(about, encoding="utf-8")
    shutil.copyfile(Path(__file__).resolve(), output / "UNPACK.py")
    return manifest


def _load_manifest(capsule_dir: Path) -> dict:
    if _is_link(capsule_dir):
        raise CapsuleError("capsule directory must not be a link or junction")
    capsule_dir = capsule_dir.resolve(strict=True)
    if not capsule_dir.is_dir():
        raise CapsuleError("capsule directory must be a real local directory")
    manifest_path = capsule_dir / "CAPSULE.v1.json"
    if _is_link(manifest_path) or not manifest_path.is_file():
        raise CapsuleError("CAPSULE.v1.json is missing or linked")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise CapsuleError("CAPSULE.v1.json exceeds the manifest-size limit")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleError("CAPSULE.v1.json is not valid UTF-8 JSON") from exc
    required = {"schema", "name", "sourceRepository", "sourceRef", "sourcePath", "treeSha256", "files"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise CapsuleError("manifest has an unexpected schema")
    if manifest["schema"] != SCHEMA or not isinstance(manifest["files"], list):
        raise CapsuleError("unsupported capsule schema")
    if not isinstance(manifest["name"], str) or not re.fullmatch(r"[a-z0-9-]{1,64}", manifest["name"]):
        raise CapsuleError("manifest contains an invalid skill name")
    if manifest["sourcePath"] != manifest["name"]:
        raise CapsuleError("manifest source path must match the skill name")
    if not isinstance(manifest["sourceRepository"], str) or not manifest["sourceRepository"].startswith("https://github.com/"):
        raise CapsuleError("manifest contains an invalid source repository")
    if not isinstance(manifest["sourceRef"], str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", manifest["sourceRef"]):
        raise CapsuleError("manifest source ref must be a full Git object ID")
    if not isinstance(manifest["treeSha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["treeSha256"]):
        raise CapsuleError("manifest contains an invalid tree digest")
    if len(manifest["files"]) > MAX_FILES:
        raise CapsuleError("manifest contains too many files")
    return manifest


def reconstruct(capsule_dir: Path) -> tuple[dict, list[tuple[str, bytes]]]:
    if _is_link(capsule_dir):
        raise CapsuleError("capsule directory must not be a link or junction")
    capsule_dir = capsule_dir.resolve(strict=True)
    manifest = _load_manifest(capsule_dir)
    files: list[tuple[str, bytes]] = []
    exact: set[str] = set()
    folded: set[str] = set()
    total_size = 0
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sources", "encoding", "size", "sha256"}:
            raise CapsuleError("manifest file entry has an unexpected schema")
        relative = _safe_relative_path(item["path"])
        folded_path = unicodedata.normalize("NFC", relative).casefold()
        if relative in exact or folded_path in folded:
            raise CapsuleError(f"duplicate or case-colliding path: {relative}")
        exact.add(relative)
        folded.add(folded_path)
        if item["encoding"] not in {"utf-8", "base64"}:
            raise CapsuleError(f"unsupported encoding for {relative}")
        if not isinstance(item["size"], int) or item["size"] < 0 or item["size"] > MAX_FILE_BYTES:
            raise CapsuleError(f"invalid declared size for {relative}")
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise CapsuleError(f"invalid SHA-256 for {relative}")
        if (
            not isinstance(item["sources"], list)
            or not item["sources"]
            or len(item["sources"]) > MAX_SOURCES_PER_FILE
            or not all(isinstance(source, str) for source in item["sources"])
            or len(set(item["sources"])) != len(item["sources"])
        ):
            raise CapsuleError(f"missing payload sources for {relative}")
        payload = bytearray()
        encoded_limit = (
            item["size"]
            if item["encoding"] == "utf-8"
            else 4 * ((item["size"] + 2) // 3)
        )
        for source_name in item["sources"]:
            safe_source = _safe_relative_path(source_name)
            if "/" in safe_source:
                raise CapsuleError("payload sources must be flat filenames")
            unresolved_source = capsule_dir / safe_source
            if _is_link(unresolved_source) or not unresolved_source.is_file():
                raise CapsuleError(f"payload is missing or linked: {safe_source}")
            source_path = unresolved_source.resolve(strict=True)
            if not source_path.is_relative_to(capsule_dir):
                raise CapsuleError(f"payload escapes the capsule directory: {safe_source}")
            if source_path.stat().st_size > MAX_PAYLOAD_BYTES:
                raise CapsuleError(f"payload exceeds the {MAX_PAYLOAD_BYTES}-byte limit: {safe_source}")
            payload_size = source_path.stat().st_size
            if len(payload) + payload_size > encoded_limit:
                raise CapsuleError(f"encoded payload exceeds the declared size for {relative}")
            payload.extend(source_path.read_bytes())
        try:
            data = bytes(payload) if item["encoding"] == "utf-8" else base64.b64decode(payload, validate=True)
        except ValueError as exc:
            raise CapsuleError(f"invalid base64 payload for {relative}") from exc
        if len(data) != item["size"]:
            raise CapsuleError(f"size mismatch for {relative}")
        if _sha256(data) != item["sha256"]:
            raise CapsuleError(f"SHA-256 mismatch for {relative}")
        files.append((relative, data))
        total_size += len(data)
        if total_size > MAX_TOTAL_BYTES:
            raise CapsuleError("capsule exceeds the total-size limit")
    if tree_digest(files) != manifest["treeSha256"]:
        raise CapsuleError("tree digest mismatch")
    if not any(path == "SKILL.md" for path, _ in files):
        raise CapsuleError("capsule does not contain SKILL.md")
    return manifest, files


def unpack_capsule(capsule_dir: Path, destination: Path) -> dict:
    manifest, files = reconstruct(capsule_dir)
    destination = destination.resolve(strict=False)
    if destination.exists():
        raise CapsuleError("destination already exists")
    destination.mkdir(parents=True)
    try:
        for relative, data in files:
            target = destination.joinpath(*PurePosixPath(relative).parts).resolve(strict=False)
            if not target.is_relative_to(destination):
                raise CapsuleError(f"target escapes the destination: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return manifest


def verify_capsule(capsule_dir: Path, source: Path) -> dict:
    manifest, reconstructed = reconstruct(capsule_dir)
    source = source.resolve(strict=True)
    git_context = _git_context(source)
    expected = (
        _git_files_at_ref(source, git_context[0], manifest["sourceRef"])
        if git_context
        else source_files(source)
    )
    if reconstructed != expected:
        raise CapsuleError("capsule bytes do not match the source skill")
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--source-repo", required=True)
    build.add_argument("--source-ref", required=True)

    unpack = subparsers.add_parser("unpack")
    unpack.add_argument("--capsule-dir", type=Path, required=True)
    unpack.add_argument("--destination", type=Path, required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--capsule-dir", type=Path, required=True)
    verify.add_argument("--source", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_capsule(
                args.source,
                args.output,
                source_repo=args.source_repo,
                source_ref=args.source_ref,
            )
        elif args.command == "unpack":
            result = unpack_capsule(args.capsule_dir, args.destination)
        else:
            result = verify_capsule(args.capsule_dir, args.source)
    except (CapsuleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"name": result["name"], "treeSha256": result["treeSha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
