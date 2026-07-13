"""
Terminal colour / ANSI helpers and the shared palette for the CLIs.

The palette below is the single source of truth for colour codes, so every
tool renders the same hues. Each module decides *when* to emit them (per
stream) via ``supports_color`` — keeping the gating local while the values
stay consistent.
"""

import ctypes
import os
import re
from typing import TextIO

# --- Shared palette (raw ANSI codes) ----------------------------------------
DIM = "\033[38;5;246m"  # grey — de-emphasised text (filenames, courtesy labels)
RED = "\033[0;31m"  # missing / unknown values
ORANGE = "\033[38;5;208m"  # warnings (VFR / non-standard fps, sub-HD resolution)
UNDERLINE = "\033[4m"  # column headers
RESET = "\033[0m"  # must follow every coloured span to avoid bleeding

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def enable_ansi_on_windows() -> bool:
    """
    Enable ANSI escape processing on Windows 10+ consoles; no-op on Unix.

    On Windows we flip ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` (0x0004) on the
    stdout handle so VT-aware terminals render colour. Older Windows / non-VT
    terminals fail silently and we fall back to plain text.

    Returns:
        ``True`` if ANSI is usable (Unix always, Windows when the
        SetConsoleMode call succeeds). Pair with a stream's ``isatty()`` to
        decide whether to emit colour.
    """
    if os.name != "nt":
        return True
    try:
        kernel32 = ctypes.windll.kernel32  # windll is Windows-only
        h = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        kernel32.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return True
    except (AttributeError, OSError):
        # No console attached, missing DLL, etc. — "no colour", not fatal.
        return False


def supports_color(stream: TextIO) -> bool:
    """
    Return True if colour should be emitted on *stream*.

    Follows the NO_COLOR / FORCE_COLOR conventions (no-color.org): a non-empty
    NO_COLOR disables colour, else a non-empty FORCE_COLOR forces it, else
    colour is used when *stream* is a TTY and ANSI is available.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return stream.isatty() and enable_ansi_on_windows()


def visible_len(s: str) -> int:
    """Return the on-screen length of *s*, ignoring ANSI escape codes."""
    return len(_ANSI_RE.sub("", s))
