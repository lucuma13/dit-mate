"""Path resolution and validation shared across the dit-mate CLIs."""

import os
import sys
from pathlib import Path


def resolve_target_dirs(raw_paths: list[str] | None, *, check_writable: bool) -> list[Path]:
    """Resolve explicit directory args, or fall back to the cwd, validating each.

    Each path must exist and be a directory; with ``check_writable`` it must
    also be writable. Exits with a message on the first failure. Returns the
    resolved, validated directories.
    """
    if raw_paths:
        out: list[Path] = []
        for raw in raw_paths:
            p = Path(raw).resolve()
            if not p.exists():
                sys.exit(f"❌  Directory does not exist: {p}")
            if not p.is_dir():
                sys.exit(f"❌  Not a directory: {p}")
            if check_writable and not os.access(p, os.W_OK):
                sys.exit(f"❌  Directory is not writable: {p}")
            out.append(p)
        return out

    try:
        cwd = Path.cwd().resolve()
    except FileNotFoundError:
        sys.exit("❌  Current directory does not exist. Please change to a valid directory.")
    if not cwd.is_dir():
        sys.exit(f"❌  Current directory does not exist: {cwd}")
    if check_writable and not os.access(cwd, os.W_OK):
        sys.exit(f"❌  Current directory is not writable: {cwd}")
    return [cwd]
