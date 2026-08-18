#!/usr/bin/env python3
"""
Scanner module — reads project files and builds a context payload for analysis.

Walks a directory tree (or reads a single file), respects common ignore patterns,
prioritizes important files, and assembles everything into a structured dict that
the analyzer can feed to an LLM.
"""

import os
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# File extensions we know how to read
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.cs', '.java', '.go', '.rs',
    '.rb', '.php', '.swift', '.kt', '.scala', '.sh', '.bash', '.ps1',
    '.psm1', '.bat', '.cmd', '.r', '.sql', '.lua', '.pl', '.pm',
}

CONFIG_EXTENSIONS = {
    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
    '.xml', '.env.example', '.env.template',
}

DOC_EXTENSIONS = {'.md', '.txt', '.rst', '.adoc'}

TEMPLATE_EXTENSIONS = {
    '.html', '.css', '.scss', '.less', '.handlebars', '.hbs',
    '.ejs', '.pug', '.svg',
}

ALL_EXTENSIONS = CODE_EXTENSIONS | CONFIG_EXTENSIONS | DOC_EXTENSIONS | TEMPLATE_EXTENSIONS

# Directories to always skip
SKIP_DIRS = {
    '.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env',
    'dist', 'build', '.next', '.nuxt', 'target', 'bin', 'obj',
    '.vs', '.idea', '.vscode', '.mypy_cache', '.pytest_cache',
    '.tox', 'eggs', '.eggs', 'htmlcov', '.coverage', 'coverage',
    '.sass-cache', 'bower_components', 'jspm_packages',
    '.parcel-cache', '.cache', 'out', '.output', '.serverless',
}

# Files to always skip
SKIP_FILES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
    'pipfile.lock', 'poetry.lock', 'composer.lock',
    '.ds_store', 'thumbs.db', '.gitattributes',
}

# Files we want to read first (higher priority)
PRIORITY_NAMES = {
    'readme.md', 'readme.txt', 'readme.rst', 'readme',
    'main.py', 'app.py', 'index.ts', 'index.js', 'main.ts', 'main.js',
    'index.tsx', 'index.jsx', 'app.ts', 'app.js', 'app.tsx', 'app.jsx',
    'setup.py', 'setup.cfg', 'pyproject.toml',
    'package.json', 'cargo.toml', 'go.mod',
    'makefile', 'dockerfile', 'docker-compose.yml', 'docker-compose.yaml',
    'requirements.txt', 'requirements.in',
    'config.yaml', 'config.yml', 'config.json', 'config.py', 'config.ts',
}

# Extensionless filenames we recognize
KNOWN_EXTENSIONLESS = {
    'makefile', 'dockerfile', 'procfile', 'rakefile', 'gemfile',
    'vagrantfile', 'jenkinsfile', 'brewfile',
}

MAX_FILE_SIZE = 100 * 1024      # 100 KB per file
MAX_TOTAL_SIZE = 500 * 1024     # 500 KB total context budget


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _should_skip_dir(dirname: str) -> bool:
    return dirname.lower() in SKIP_DIRS or dirname.startswith('.')


def _should_skip_file(filename: str) -> bool:
    return filename.lower() in SKIP_FILES


def _should_include_file(filepath: Path) -> bool:
    if filepath.suffix.lower() in ALL_EXTENSIONS:
        return True
    if filepath.name.lower() in PRIORITY_NAMES:
        return True
    if filepath.suffix == '' and filepath.name.lower() in KNOWN_EXTENSIONLESS:
        return True
    return False


def _read_file_safe(filepath: Path) -> Optional[str]:
    """Read a text file. Returns None for binary/unreadable files."""
    try:
        size = filepath.stat().st_size
        if size > MAX_FILE_SIZE:
            return f"[File too large: {size / 1024:.1f} KB — skipped]"
        if size == 0:
            return "[Empty file]"
        with open(filepath, 'r', encoding='utf-8', errors='strict') as f:
            return f.read()
    except (UnicodeDecodeError, PermissionError, OSError):
        return None


def _build_tree_string(root: Path, all_paths: list) -> str:
    """Build a clean ASCII directory tree from a list of (filepath, relative) tuples."""
    lines = [f"{root.name}/"]
    seen_dirs: set[tuple] = set()

    sorted_paths = sorted(all_paths, key=lambda x: str(x[1]))

    for _filepath, relative in sorted_paths:
        parts = relative.parts

        # Print parent directories we haven't seen yet
        for depth in range(len(parts) - 1):
            dir_key = parts[: depth + 1]
            if dir_key not in seen_dirs:
                seen_dirs.add(dir_key)
                indent = "    " * depth
                lines.append(f"{indent}+-- {parts[depth]}/")

        # Print the file itself
        indent = "    " * (len(parts) - 1)
        lines.append(f"{indent}+-- {parts[-1]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_project(root: Path) -> dict:
    """Scan a project directory and return a context payload."""
    root = root.resolve()
    files: list[dict] = []
    total_size = 0
    skipped_files: list[str] = []

    # Collect all candidate paths
    all_paths: list[tuple[Path, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune directories in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]

        for filename in filenames:
            filepath = Path(dirpath) / filename
            relative = filepath.relative_to(root)

            if _should_skip_file(filename):
                continue
            if not _should_include_file(filepath):
                skipped_files.append(str(relative))
                continue

            all_paths.append((filepath, relative))

    # Sort — priority files first, then alphabetically
    def _sort_key(item):
        fp, rel = item
        is_priority = fp.name.lower() in PRIORITY_NAMES
        return (0 if is_priority else 1, str(rel))

    all_paths.sort(key=_sort_key)

    # Read files, respecting the total budget
    for filepath, relative in all_paths:
        if total_size >= MAX_TOTAL_SIZE:
            skipped_files.append(f"{relative} [budget exceeded]")
            continue

        content = _read_file_safe(filepath)
        if content is None:
            skipped_files.append(f"{relative} [binary/unreadable]")
            continue

        file_size = len(content.encode('utf-8'))
        total_size += file_size

        files.append({
            "relative_path": str(relative).replace("\\", "/"),
            "extension": filepath.suffix,
            "size_bytes": file_size,
            "content": content,
        })

    tree = _build_tree_string(root, all_paths)

    return {
        "project_name": root.name,
        "root": str(root),
        "is_single_file": False,
        "tree": tree,
        "files": files,
        "skipped_files": skipped_files,
        "file_count": len(files),
        "total_size_kb": total_size / 1024,
    }


def scan_file(filepath: Path) -> dict:
    """Scan a single file and return a context payload."""
    filepath = filepath.resolve()
    content = _read_file_safe(filepath)

    if content is None:
        print(f"Error: Cannot read '{filepath}' (binary or unreadable)")
        sys.exit(1)

    file_size = len(content.encode('utf-8'))

    return {
        "project_name": filepath.name,
        "root": str(filepath.parent),
        "is_single_file": True,
        "tree": filepath.name,
        "files": [{
            "relative_path": filepath.name,
            "extension": filepath.suffix,
            "size_bytes": file_size,
            "content": content,
        }],
        "skipped_files": [],
        "file_count": 1,
        "total_size_kb": file_size / 1024,
    }
