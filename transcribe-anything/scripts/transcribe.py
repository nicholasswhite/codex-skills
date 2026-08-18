#!/usr/bin/env python3
"""Locate the project and delegate to its public CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _project_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src").is_dir():
            return parent
    raise SystemExit(
        "Could not locate the transcribe-anything project. Keep this skill inside "
        "the project or install the transcribe-anything CLI."
    )


def main() -> int:
    root = _project_root()
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
    )
    project_python = next((path for path in candidates if path.is_file()), None)
    if project_python is not None:
        try:
            running_python = Path(sys.executable).resolve()
        except OSError:
            running_python = Path(sys.executable)
        if running_python != project_python.resolve():
            completed = subprocess.run(
                [str(project_python), "-m", "transcribe_anything.cli", *sys.argv[1:]],
                cwd=root,
                check=False,
            )
            return completed.returncode

    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from transcribe_anything.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
