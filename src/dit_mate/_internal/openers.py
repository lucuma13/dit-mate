"""Open files in the OS default app or the user's editor."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


def maybe_open_config(
    path: Path,
    *,
    open_app: bool,
    edit: bool,
    label: str,
    ensure: Callable[[], None] | None = None,
) -> bool:
    """Handle the -O/-E config-opening flags. Returns True if one fired
    (the caller should return immediately).

    The flags themselves stay declared in each tool's parser — this helper
    only takes the parsed booleans, so each script remains the single,
    complete declaration of its CLI surface.

    ``ensure`` is called to create the file if missing (xpandroll's blank
    TSV); without it, a missing file is a hard error (mkday/mrl presets).
    """
    if not (open_app or edit):
        return False
    if ensure is not None:
        ensure()
    elif not path.exists():
        sys.exit(f"❌  Config file not found: {path}")
    if open_app:
        open_in_default_app(path, label=label)
    else:
        print(f"📋  {label.capitalize()}: {path}")
        open_in_editor(path, label=label)
    return True


def open_in_default_app(path: Path, *, label: str) -> None:
    """Open *path* in the OS default application for its file type.

    macOS:   ``open`` → TextEdit fallback when no app is registered.
    Windows: ``os.startfile`` → Notepad fallback.
    Linux:   ``xdg-open``; if it fails (no handler / not installed), exit and
             point the user at the ``-E`` (editor) option.

    Assumes *path* exists (callers handle existence/creation). ``label`` is the
    human description used in messages, e.g. ``"presets config file"``.
    """
    print(f"📋  Opening {label}: {path}")

    if sys.platform == "darwin":
        result = subprocess.run(["open", str(path)], capture_output=True, check=False)
        if result.returncode == 0:
            return
        # No app registered for this type — fall back to TextEdit explicitly.
        print(f"No app registered — opening {label} with TextEdit")
        result = subprocess.run(["open", "-a", "TextEdit", str(path)], capture_output=True, check=False)
        if result.returncode != 0:
            sys.exit(
                f"❌  Could not open {label} with TextEdit either.\n    Use -E to open it in your $EDITOR instead."
            )
        return

    if sys.platform == "win32":
        try:
            os.startfile(str(path))
        except OSError:
            subprocess.Popen(["notepad.exe", str(path)])
        return

    # Linux / other: xdg-open is the freedesktop default-app mechanism.
    try:
        result = subprocess.run(["xdg-open", str(path)], timeout=5, capture_output=True, check=False)
        if result.returncode == 0:
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    sys.exit(f"❌  Could not open {label} with xdg-open.\n    Use -E to open it in your $EDITOR instead.")


def open_in_editor(path: Path, *, label: str) -> None:
    """Open *path* in the user's ``$EDITOR``/``$VISUAL`` (nano/notepad fallback).

    Replaces the current process via ``os.execvp``. On a missing editor, exit
    and suggest setting ``$EDITOR`` or using the ``-O`` (default-app) option.
    ``label`` is the human description used in messages.
    """
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or ("notepad" if sys.platform == "win32" else "nano")
    print(f"📝  Opening {path} with '{editor}'…")
    try:
        os.execvp(editor, [editor, str(path)])
    except FileNotFoundError:
        sys.exit(
            f"❌  Editor not found: '{editor}'\n"
            f"    Set the $EDITOR environment variable to your preferred editor,\n"
            f"    or use -O to open {label} with the system default app."
        )
