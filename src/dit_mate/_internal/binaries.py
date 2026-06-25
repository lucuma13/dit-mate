"""Invoke the external CLI binaries the dit-mate tools depend on.

Single chokepoint for shelling out to third-party executables
(``mediainfo``, ``exiftool``, ``ffprobe``, clipboard tools). Centralises the
Windows console-suppression flag and the "never raise" capture contract so call
sites don't repeat them.
"""

import os
import subprocess

# On Windows, child processes default to opening their own console window —
# annoying for a CLI tool. CREATE_NO_WINDOW (0x08000000) suppresses that without
# affecting stdout/stderr capture. 0 is an inert no-op on POSIX, so invoke() can
# pass it unconditionally instead of splatting a platform-dependent dict.
_CREATION_FLAGS = 0x08000000 if os.name == "nt" else 0


def invoke(
    cmd: list[str],
    *,
    capture_output: bool = False,
    text: bool = False,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Run an external tool via ``subprocess.run`` with the no-window flag applied.

    Always passes ``check=False`` and the Windows console-suppression flag, so no
    call site has to remember either. ``cmd`` is an argv list (no shell), so paths
    with spaces/quotes/unicode are safe. ``stdin_text`` is fed to the process's
    stdin (subprocess's ``input``).
    """
    return subprocess.run(
        cmd,
        check=False,
        creationflags=_CREATION_FLAGS,
        capture_output=capture_output,
        text=text,
        input=stdin_text,
    )


def run_capture(cmd: list[str]) -> str:
    """Run *cmd* and return its stdout; empty string on any failure.

    Captures both streams and never raises. On any failure (tool not found,
    OS error, non-zero exit) returns ``""`` so callers can treat "no data"
    and "tool not installed" identically rather than crashing.
    """
    try:
        return invoke(cmd, capture_output=True, text=True).stdout
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""
