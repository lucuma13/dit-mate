#!/usr/bin/env python3
"""basicmeta — basic metadata utility for sanity-checking original camera files.

`basicmeta` lists the essential metadata that needs to be consistent across all
cameras in a given shooting day: frame rate, resolution and recorded date. It
integrates [Media-Info](https://github.com/mediaarea/mediainfo) and
[ExifTool](https://github.com/exiftool/exiftool) to support all professional
camera acquisition formats (MXF, MOV, MP4, R3D, and BWF WAV audio), as well as
some extra video containers (MKV, AVI, M4V, MTS, FLV, WebM).

Examples:

Check the essential metadata of multiple camera rolls:

```
basicmeta "path/to/rushes/"
basicmeta                                   # scans the current directory
```
"""
# Copyright (c) 2026 Luis Gómez Gutiérrez. License: MIT.

import argparse
import ctypes
import importlib.metadata
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

# -----------------------------------------------------------------------------
# Version
# -----------------------------------------------------------------------------

try:
    __version__ = importlib.metadata.version("dit-mate")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

#: Extensions that are *always* parsed (genuine camera containers).
CAMERA_VIDEO_EXTS = {"mxf", "mp4", "mov", "insv"}

#: Extensions that are only parsed when the user passes ``-f``.
#: These are common but generic, and seeing them next to camera originals
#: usually indicates the wrong files made it onto a card. The default is to
#: skip them silently; ``-f`` opts in.
OTHER_VIDEO_EXTS = {"mkv", "avi", "m4v", "mts", "flv", "webm"}

#: Union of both video buckets — handled by ``mediainfo`` either way.
MEDIAINFO_EXTS = CAMERA_VIDEO_EXTS | OTHER_VIDEO_EXTS


# ---------------------------------------------------------------------------
# Terminal color setup
# ---------------------------------------------------------------------------


def _enable_ansi_on_windows() -> bool:
    """Enable ANSI escape processing on Windows 10+ consoles.

    On Linux and macOS this is a no-op that always returns True. On Windows
    we flip ``ENABLE_VIRTUAL_TERMINAL_PROCESSING`` (0x0004) on the standard
    output handle so that terminals built into Windows 10+ render our color
    codes correctly. Older Windows or terminals that don't support VT mode
    silently fail and we'll fall back to plain text.

    Returns:
        True if ANSI processing is available (or we're not on Windows),
        False if we're on Windows and couldn't enable it.
    """
    if os.name != "nt":
        # Unix-like terminals understand ANSI by default.
        return True
    try:
        # ctypes is in the stdlib; we use it to call the Win32 console API
        # directly so we don't pull in colorama as a third-party dep.
        kernel32 = ctypes.windll.kernel32
        # GetStdHandle(-11) == STD_OUTPUT_HANDLE
        h = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # OR in ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) without dropping
        # any modes that were already set.
        kernel32.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        # Anything unexpected (no console attached, missing DLL, etc.) just
        # means "no color" — not a fatal error.
        return False


# We only emit color when stdout is a TTY *and* ANSI is supported.
# Piping to a file or another process drops color automatically.
_ANSI_OK = sys.stdout.isatty() and _enable_ansi_on_windows()

#: Dim grey color (filename suffix). Empty string when colors are disabled,
#: which lets the format strings stay uniform without conditional branches.
DIM = "\033[38;5;246m" if _ANSI_OK else ""

#: Red color (used to flag missing/unknown fields).
RED = "\033[0;31m" if _ANSI_OK else ""

#: ANSI reset sequence — must follow every colored span to avoid bleeding.

#: Orange color (used to flag VFR and non-standard frame rates).
ORANGE = "\033[38;5;208m" if _ANSI_OK else ""
RESET = "\033[0m" if _ANSI_OK else ""

#: Broadcast-standard frame rates. fps strings not in this set are
#: coloured orange as a heads-up that the rate is unusual.
KNOWN_FPS = {
    "23.976",
    "23.97",
    "24",
    "25",
    "29.970",
    "29.97",
    "30",
    "50",
    "59.940",
    "59.94",
    "60",
}


# ---------------------------------------------------------------------------
# Column layout constants
# ---------------------------------------------------------------------------

#: DATE always starts at this terminal column (0-indexed visible position).
#: Filename always starts at _COL_FILE. These are the only two fixed anchors;
#: everything to the left of DATE is laid out dynamically using hyphen
#: separators (see ``_render_line``).
_COL_DATE = 25
_COL_FILE = 37  # col of "(" in "  (filename)"

#: Compiled pattern for stripping ANSI escape sequences before measuring
#: visible string length. Color codes inflate len() but take no screen space.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _vis(s: str) -> int:
    """Return the visible (on-screen) length of *s*, ignoring ANSI codes."""
    return len(_ANSI_RE.sub("", s))


def _distribute_hyphens(total: int, n_gaps: int) -> list[int]:
    """Split *total* hyphens evenly across *n_gaps* separators.

    Any remainder is given to the leftmost gap. No taper is applied —
    separators are as equal as possible, matching the expected output style
    where both gaps get the same number of hyphens when total is even.
    """
    if n_gaps == 0:
        return []
    if n_gaps == 1:
        return [total]
    base, rem = divmod(total, n_gaps)
    return [base + (1 if i < rem else 0) for i in range(n_gaps)]


def _res_is_low(res_raw: str) -> bool:
    """Return True if resolution is below Full HD (excludes Audio/empty)."""
    FULL_HD_WIDTH = 1920
    FULL_HD_HEIGHT = 1080

    if not res_raw or res_raw == "Audio":
        return False
    try:
        w, h = (int(x.strip()) for x in res_raw.split("x", 1))
        return w < FULL_HD_WIDTH or h < FULL_HD_HEIGHT
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Compiled regular expressions (compile once at import time)
# ---------------------------------------------------------------------------

#: Matches the leading ``YYYY-MM-DD`` or ``YYYY:MM:DD`` of a date string.
#: Both separators appear in the wild — exiftool emits ``:`` while
#: mediainfo emits ``-``.
EXIF_DATE_RE = re.compile(r"^(\d{4})[:-](\d{2})[:-](\d{2})")

#: Cheap "does this string contain at least one non-zero digit" test, used
#: to skip BWF placeholder dates like ``0000-00-00``.
HAS_NONZERO_DIGIT = re.compile(r"[1-9]")


# ---------------------------------------------------------------------------
# Subprocess invocation defaults (Windows-specific tweaks)
# ---------------------------------------------------------------------------

# On Windows, child processes default to opening their own console window —
# annoying for a CLI tool. CREATE_NO_WINDOW (0x08000000) suppresses that
# without affecting stdout/stderr capture.
_POPEN_KW: dict = {}
if os.name == "nt":
    _POPEN_KW["creationflags"] = 0x08000000


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str]) -> str:
    """Run a command and return its stdout as a string.

    Captures both streams, never raises. On any failure (tool not found,
    OS error, non-zero exit, broken pipe, ...) returns an empty string.
    Callers are expected to treat empty output as "no data".

    Args:
        cmd: argv list. The first element is the program name; remaining
            elements are arguments. Passed directly to ``subprocess.run``
            with no shell interpretation, so paths with spaces, quotes,
            or unicode are safe.

    Returns:
        Captured stdout, or ``""`` on failure.
    """
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,  # decode stdout/stderr as utf-8
            check=False,  # don't raise on non-zero exit
            **_POPEN_KW,  # CREATE_NO_WINDOW on Windows
        )
        return r.stdout
    except FileNotFoundError:
        # The tool isn't installed. Caller will format every line as
        # "Unknown" rather than crashing the run.
        return ""
    except (OSError, subprocess.SubprocessError):
        # Misc OS-level failures — same fallback path.
        return ""


# ---------------------------------------------------------------------------
# Field normalization
# ---------------------------------------------------------------------------


def _norm_fps(s: str) -> str:
    """Normalize a frame-rate value to the display form used by basicmeta.

    Behavior matches the original Bash version: parses any numeric input,
    formats with three decimal places, then strips a trailing ``.000`` so
    integer rates render as ``"24"`` instead of ``"24.000"``. Non-numeric
    input is returned unchanged (apart from leading/trailing whitespace).

    Args:
        s: raw frame rate as reported by mediainfo or exiftool.

    Returns:
        ``""`` if the input was empty/whitespace, otherwise a clean
        display string like ``"23.976"``, ``"24"``, or ``"29.970"``.
    """
    s = (s or "").strip()
    if not s:
        return ""
    try:
        # Format with 3 decimals to match the original's printf "%.3f",
        # then strip ".000" so integer rates display compactly.
        f = float(s)
        out = f"{f:.3f}"
        return out.removesuffix(".000")
    except ValueError:
        # Non-numeric values (rare — usually a sentinel like "VFR")
        # pass through, with the same .000 trimming applied textually.
        return s.removesuffix(".000")


def _norm_date_iso(s: str) -> str:
    """Extract a YYYY-MM-DD date from a metadata timestamp.

    Accepts both ``YYYY-MM-DD...`` and ``YYYY:MM:DD...`` forms (mediainfo
    uses the former, exiftool the latter). Strips off any trailing time
    portion. Crucially, **rejects all-zero dates** like ``0000-00-00`` or
    ``0-00-00`` which some MXF files carry as a "no data" placeholder —
    the original Bash version displayed those as if they were real dates.

    Args:
        s: raw timestamp string.

    Returns:
        ``"YYYY-MM-DD"`` on success, ``""`` if the input doesn't look like
        a date or represents the all-zero placeholder.
    """
    if not s:
        return ""
    m = EXIF_DATE_RE.match(s)
    if not m:
        return ""
    y, mo, d = m.groups()
    # Reject the all-zero placeholder. The regex's \d{4} matches "0000"
    # so we coerce to int and check explicitly.
    if int(y) == 0 and int(mo) == 0 and int(d) == 0:
        return ""
    return f"{y}-{mo}-{d}"


# ---------------------------------------------------------------------------
# MediaInfo: ONE call for all video files; parse JSON array
# ---------------------------------------------------------------------------


def batch_mediainfo(paths: list[Path]) -> dict[Path, str]:
    """Run mediainfo once for every video file and format one line each.

    This is the function that powers the rewrite's headline performance
    win. Instead of invoking mediainfo per file (the original behavior),
    we pass *all* the paths in a single command and let mediainfo emit a
    JSON array — one entry per input — that we then parse in Python.

    The fields we extract:

    - ``General/FrameRate``         -> displayed fps
    - ``General/Encoded_Date``      -> ISO date (zero-dates rejected)
    - ``General/FileNameExtension`` -> filename suffix shown at end of line
    - ``Video/Width``, ``Video/Height`` -> "WIDTH x HEIGHT" resolution

    Args:
        paths: video files to analyze. May be empty; an empty dict is
            returned in that case (no subprocess is launched).

    Returns:
        A dict mapping each input path to a 4-tuple
        ``(fps_raw, res_raw, date_raw, name)`` — all plain strings
        for the caller to render with the active field list.
    """
    if not paths:
        return {}

    raw = _run(["mediainfo", "--Output=JSON", *map(str, paths)])
    results: dict[Path, tuple[str, str, str, str]] = {}

    # If mediainfo isn't installed or the call failed entirely, we still
    # emit one "Unknown" line per requested path so the user sees that
    # *something* was scanned, rather than silently dropping files.
    if not raw.strip():
        for p in paths:
            results[p] = ("", "", "", "", p.name)
        return results

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Malformed output — treat as if the call failed.
        for p in paths:
            results[p] = ("", "", "", "", p.name)
        return results

    # mediainfo returns a single dict for one input but a list of dicts for
    # multiple inputs. Normalize so the rest of the function can iterate.
    entries = parsed if isinstance(parsed, list) else [parsed]

    # Build a fast lookup table from the path mediainfo echoed back ("@ref")
    # to its parsed entry. We normalize via os.path.normpath so that, e.g.,
    # "/foo/./bar.mov" matches "/foo/bar.mov" and Windows backslashes vs
    # forward slashes don't trip us up.
    by_path: dict[str, dict] = {}
    for e in entries:
        ref = e.get("media", {}).get("@ref", "")
        if ref:
            by_path[os.path.normpath(ref)] = e

    # Iterate in *input* order so the output is deterministic regardless
    # of how mediainfo ordered its array internally.
    for p in paths:
        e = by_path.get(os.path.normpath(str(p)))
        if not e:
            # mediainfo didn't return anything for this file (e.g. it
            # couldn't parse the container at all). Emit a placeholder.
            results[p] = ("", "", "", "", p.name)
            continue

        # mediainfo's JSON groups properties under a list of "tracks", each
        # tagged by @type. We need exactly the General and Video tracks.
        tracks = e.get("media", {}).get("track", [])
        gen = next((t for t in tracks if t.get("@type") == "General"), {})
        vid = next((t for t in tracks if t.get("@type") == "Video"), {})

        fps_raw = _norm_fps(gen.get("FrameRate", ""))
        fps_mode = gen.get("FrameRate_Mode", "")
        # Use ONLY Encoded_Date — never File_Modified_Date, which is just
        # the filesystem mtime and has nothing to do with the recording.
        date = _norm_date_iso(gen.get("Encoded_Date", ""))

        # mediainfo *usually* emits Width/Height as numeric strings, but
        # version differences sometimes give us numbers. Coerce defensively.
        w_raw = vid.get("Width", "")
        h_raw = vid.get("Height", "")
        w = w_raw.strip() if isinstance(w_raw, str) else str(w_raw).strip()
        h = h_raw.strip() if isinstance(h_raw, str) else str(h_raw).strip()
        res = f"{w} x {h}" if (w and h) else ""
        res_colour = ORANGE if _res_is_low(res) else ""

        # FileNameExtension is the bare filename (e.g. "clip_001.mov"), not
        # just the extension — confusing but matches the original tool.
        ext_name = gen.get("FileNameExtension") or p.name

        coloured_res = f"{res_colour}{res}{RESET if res_colour else ''}"
        results[p] = (
            fps_raw,
            coloured_res if res_colour else res,
            date,
            "",
            ext_name or p.name,
        )

    return results


def _render_line(
    fps_raw: str,
    res_raw: str,
    date_raw: str,
    sn: str,
    name: str,
    fields: list[str],
    *,
    audio: bool = False,
) -> str:
    """Assemble one output line from raw field values.

    Columns are anchored by fixed visible-column positions: DATE always starts
    at ``_COL_DATE`` (col 27) and the filename always starts at ``_COL_FILE``
    (col 39). The separators between fields are hyphen runs of the form
    " ----- " whose lengths are computed dynamically so that these anchors
    are always hit, regardless of which fields are active or how wide their
    values are. ANSI colour codes are stripped before measuring widths so
    that coloured values (orange fps, red Unknown) align correctly.

    Args:
        fps_raw:  plain fps string, e.g. ``"23.976"`` (colour added here).
        res_raw:  plain resolution string, e.g. ``"3840 x 2160"``.
        date_raw: plain ISO date string, e.g. ``"2024-06-15"``.
        sn:       camera serial number string, or ``""`` if absent/not shown.
        name:     filename shown in the trailing dim parens.
        fields:   ordered list of field keys to include
                  (``"fps"``, ``"resolution"``, ``"date"``, ``"sn"``).
        audio:    when True ``"resolution"`` renders as ``"Audio"``.

    Returns:
        The formatted single-line string ready for printing.
    """
    # Build (coloured_value, visible_width, field_name) for each active field.
    coloured: list[tuple[str, int, str]] = []
    for field in fields:
        if field == "fps":
            if fps_raw:
                c = ORANGE if fps_raw not in KNOWN_FPS else ""
                val = f"{c}{fps_raw}{RESET if c else ''} fps"
            else:
                val = f"{RED}Unknown{RESET} fps"
            coloured.append((val, _vis(val), "fps"))

        elif field == "resolution":
            if audio:
                val = "Audio"
            elif res_raw:
                c = ORANGE if _res_is_low(res_raw) else ""
                val = f"{c}{res_raw}{RESET if c else ''}"
            else:
                val = f"{RED}Unknown{RESET}"
            coloured.append((val, _vis(val), "resolution"))

        elif field == "date":
            val = date_raw or f"{RED}Unknown{RESET}"
            coloured.append((val, _vis(val), "date"))

        elif field == "sn":
            val = sn or f"{RED}Unknown{RESET}"
            coloured.append((val, _vis(val), "sn"))

    if not coloured:
        return f"{DIM}({name}){RESET}"

    n = len(coloured)

    # Identify the rightmost anchor field: DATE (fixed at _COL_DATE) takes
    # priority; otherwise the rightmost field anchors to _COL_FILE - 2
    # so that "  (name)" starts exactly at _COL_FILE.
    anchor_idx = next((i for i, (_, _, f) in enumerate(coloured) if f == "date"), n - 1)
    anchor_col = _COL_DATE if coloured[anchor_idx][2] == "date" else _COL_FILE - 2 - coloured[anchor_idx][1]

    # Total visible width of all fields to the left of (and including) the anchor.
    left_vals_width = sum(w for _, w, _ in coloured[:anchor_idx])
    n_gaps = anchor_idx  # number of separators to the left of the anchor field
    total_hyphens = anchor_col - left_vals_width - n_gaps * 2

    # Compute hyphens per gap.
    # Minimum is 1 hyphen per gap so there's always visible separation;
    # if even that isn't possible (values too wide) fall back to 2 spaces.
    if n_gaps > 0 and total_hyphens >= n_gaps:
        h = _distribute_hyphens(total_hyphens, n_gaps)
        seps = [" " + "-" * hi + " " for hi in h]
    elif n_gaps > 0:
        # Values too wide to fit anchor — use 2 spaces, let date shift right.
        seps = ["  "] * n_gaps
    else:
        seps = []

    # Assemble left side (up to and including the anchor field).
    result = ""
    for i in range(anchor_idx + 1):
        if i > 0:
            result += seps[i - 1]
        result += coloured[i][0]

    # Append any fields that come after the anchor (e.g. sn after date)
    # with simple two-space separators — these are rare/unanchored.
    for i in range(anchor_idx + 1, n):
        result += "  " + coloured[i][0]

    # Pad to _COL_FILE - 2 so filename always starts at _COL_FILE.
    visible_len = _vis(result)
    pad = _COL_FILE - 2 - visible_len
    result += " " * max(2, pad)

    return result + f"{DIM}({name}){RESET}"


# ---------------------------------------------------------------------------
# Exiftool batched output parsing
# ---------------------------------------------------------------------------


def _split_exiftool_batches(out: str, n_files: int) -> list[dict[str, str]]:
    """Parse exiftool's multi-file output into one record per file.

    When exiftool processes multiple files in a single invocation, it
    separates each file's output with a header line of the form::

        ======== /path/to/the/file.ext

    For a *single* file, no such header is emitted — the output is just
    the bare key/value lines. We have to handle both shapes.

    Args:
        out: raw exiftool stdout.
        n_files: number of files that were passed to exiftool. Used to
            distinguish the "no header" single-file case from the
            "headered" multi-file case.

    Returns:
        A list of records. Each record is a flat ``{key: value}`` dict
        of the metadata fields exiftool emitted for that file. When a
        ``======== <path>`` header is present, the path is stored under
        the synthetic key ``"_file"`` so the caller can map records back
        to inputs.
    """
    if n_files <= 1:
        # Single-file path: no header markers, just plain key:value lines.
        rec = _parse_exiftool_block(out)
        return [rec] if rec else []

    records: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in out.splitlines():
        if line.startswith("======== "):
            # Start a new record. Flush the previous one if it had any
            # content. The first iteration's "cur" is empty so nothing
            # is flushed prematurely.
            if cur:
                records.append(cur)
            # Stash the file path under a private key so we can match
            # records to input paths later. exiftool sometimes adds a
            # trailing colon to the path on this line — strip generously.
            cur = {"_file": line[len("======== ") :].rstrip(":").strip()}
        elif ":" in line:
            # exiftool with -s2 emits "Tag: value" lines (no padding).
            # ``partition`` cleanly splits on the *first* colon only,
            # which matters because some values themselves contain colons
            # (e.g. timestamps like "2024:06:15 10:30:00").
            k, _, v = line.partition(":")
            cur[k.strip()] = v.strip()
    # Don't forget the final record after the loop ends.
    if cur:
        records.append(cur)
    return records


def _parse_exiftool_block(text: str) -> dict[str, str]:
    """Parse a single exiftool output block (no ``========`` headers).

    Args:
        text: stdout from a one-file exiftool invocation.

    Returns:
        Flat dict of ``{tag: value}``. Empty if no key:value lines were
        present.
    """
    rec: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            rec[k.strip()] = v.strip()
    return rec


def batch_r3d(paths: list[Path]) -> dict[Path, str]:
    """Process all R3D files in a single exiftool call.

    R3D is RED Camera's proprietary format, not handled by mediainfo.
    Exiftool reads its tags reliably but, like all Perl-based tools,
    pays a hefty cold-start cost (~200 ms). Batching every R3D file
    into one invocation eliminates N-1 of those startups.

    The fields we extract:

    - ``FrameRate``                  -> displayed fps
    - ``DateTimeOriginal``           -> ISO date
    - ``ImageWidth`` / ``ImageHeight`` -> resolution
    - ``Filename``                   -> display name

    Args:
        paths: R3D files to analyze. May be empty.

    Returns:
        A dict mapping each input path to a 4-tuple
        ``(line, fps_raw, res_raw, date_raw)`` for consistency checking.
    """
    if not paths:
        return {}

    out = _run(
        [
            "exiftool",
            "-s2",
            "-m",
            "-FrameRate",
            "-DateTimeOriginal",
            "-ImageWidth",
            "-ImageHeight",
            "-SerialNumber",
            "-Filename",
            *map(str, paths),
        ]
    )
    recs = _split_exiftool_batches(out, len(paths))

    # Build a path -> record lookup. With >=2 files, exiftool's "========"
    # headers give us the path explicitly; with exactly 1 file we trust
    # positional alignment instead.
    by_file: dict[str, dict[str, str]] = {}
    for rec in recs:
        f = rec.get("_file")
        if f:
            by_file[os.path.normpath(f)] = rec

    results: dict[Path, tuple[str, str, str, str]] = {}
    for p in paths:
        rec = by_file.get(os.path.normpath(str(p)))
        if rec is None and len(paths) == 1 and recs:
            # Single-file fallback: there's only one record and it has no
            # path header, so it must be for our single input.
            rec = recs[0]
        if rec is None:
            rec = {}

        fps = _norm_fps(rec.get("FrameRate", ""))
        date = _norm_date_iso(rec.get("DateTimeOriginal", ""))
        w = rec.get("ImageWidth", "")
        h = rec.get("ImageHeight", "")
        # exiftool emits "FileName" (capital N); some older versions used
        # "Filename" (lowercase). Accept either.
        name = rec.get("FileName") or rec.get("Filename") or p.name

        sn = rec.get("SerialNumber", "")
        res = f"{w} x {h}" if (w and h) else ""
        results[p] = (fps, res, date, sn, name)
    return results


def batch_wav(paths: list[Path]) -> dict[Path, str]:
    """Process all WAV files in a single exiftool call.

    Production audio WAVs (BWF — Broadcast Wave Format) carry timecode
    and date info in iXML or BEXT chunks. Different recorders write the
    same logical fields under different tags, so we probe several aliases
    for both the frame rate (timecode rate) and the date, taking the
    first one that has a usable value.

    Args:
        paths: WAV files to analyze. May be empty.

    Returns:
        A dict mapping each input path to a 4-tuple
        ``(line, fps_raw, "Audio", date_raw)`` for consistency checking.
    """
    if not paths:
        return {}

    out = _run(
        [
            "exiftool",
            "-s",
            "-s",
            # Frame-rate aliases, in order of preference. The first non-empty
            # value wins. iXML's BWF tags are the most authoritative for
            # location-sound recordings.
            "-BwfxmlSpeedTimecodeRate",
            "-iXML:SampleRate",
            "-Speed",
            "-VideoFrameRate",
            # Date aliases. We probe all of them and take the first that
            # contains real (non-zero) digits, since some recorders write
            # placeholder dates when no system clock is set.
            "-DateTimeOriginal",
            "-DateCreated",
            "-BwfxmlBextBwfOriginationDate",
            "-Filename",
            *map(str, paths),
        ]
    )
    recs = _split_exiftool_batches(out, len(paths))

    # Probe order matters: try the most-specific tags first so a recorder
    # that writes both BWF timecode rate and a generic VideoFrameRate ends
    # up with the BWF value (which is the intended audio reference).
    fps_keys = ("BwfxmlSpeedTimecodeRate", "VideoFrameRate", "Speed", "iXML")
    date_keys = (
        "DateTimeOriginal",
        "DateCreated",
        "OriginatorReference",
        "BwfxmlBextBwfOriginationDate",
    )

    by_file: dict[str, dict[str, str]] = {}
    for rec in recs:
        f = rec.get("_file")
        if f:
            by_file[os.path.normpath(f)] = rec

    results: dict[Path, tuple[str, str, str, str]] = {}
    for p in paths:
        rec = by_file.get(os.path.normpath(str(p)))
        if rec is None and len(paths) == 1 and recs:
            rec = recs[0]
        if rec is None:
            rec = {}

        # First non-empty FPS-equivalent tag wins.
        fps = ""
        for k in fps_keys:
            v = rec.get(k, "")
            if v:
                fps = _norm_fps(v)
                if fps:
                    break

        # First date tag whose value contains at least one non-zero
        # digit wins (filters out "0000-00-00" placeholders without
        # needing a full parse on every candidate).
        date = ""
        for k in date_keys:
            v = rec.get(k, "")
            if v and HAS_NONZERO_DIGIT.search(v):
                d = _norm_date_iso(v)
                if d:
                    date = d
                    break

        name = rec.get("FileName") or rec.get("Filename") or p.name
        results[p] = (fps, "Audio", date, "", name)
    return results


# ---------------------------------------------------------------------------
# Driver: directory walk, dispatch, and output assembly
# ---------------------------------------------------------------------------


def batch_sn(paths: list[Path]) -> dict[Path, str]:
    """Fetch camera serial numbers via exiftool.

    This is a separate, on-demand exiftool call issued only when the user
    explicitly passes ``--sn`` / ``--serialnumber``. It is never called in
    default mode to avoid the Perl cold-start penalty for a field the user
    didn't ask for.

    For R3D files the serial number is already fetched inside ``batch_r3d``
    as part of its normal exiftool call, so ``batch_sn`` is only invoked
    for mediainfo-handled files (MP4, MOV, MXF) and WAV.

    Args:
        paths: files to probe. May be empty.

    Returns:
        Dict mapping each path to its serial number string (empty string
        when the tag is absent or the file type doesn't carry it).
    """
    if not paths:
        return {}

    out = _run(
        [
            "exiftool",
            "-json",
            "-m",
            "-CameraSerialNumber",
            "-SerialNumber",
            "-BwfxmlUserTserial",
            *map(str, paths),
        ]
    )

    result: dict[Path, str] = {}
    if out.strip():
        try:
            entries = json.loads(out)
        except json.JSONDecodeError:
            entries = []
        # exiftool always includes "SourceFile" in -json output — use it
        # to key records back to our input paths.
        by_path: dict[str, dict] = {os.path.normpath(e["SourceFile"]): e for e in entries if "SourceFile" in e}
        for p in paths:
            rec = by_path.get(os.path.normpath(str(p)), {})
            sn = rec.get("CameraSerialNumber") or rec.get("SerialNumber") or rec.get("BwfxmlUserTserial") or ""
            result[p] = sn

    for p in paths:
        result.setdefault(p, "")
    return result


# ---------------------------------------------------------------------------
# Driver: directory walk, dispatch, and output assembly
# ---------------------------------------------------------------------------


def collect_files_by_subdir(
    target: Path,
    force: bool,
) -> list[tuple[Path, list[Path], list[Path], list[Path]]]:
    """Walk ``target`` recursively and bucket files by handler, grouped by subdirectory.

    Each subdirectory encountered (including ``target`` itself) becomes one
    entry in the returned list, ordered depth-first alphabetically. This lets
    ``_batch_groups`` merge lone-clip subdirectories before dispatch.

    Args:
        target: directory to walk. Must already be a directory.
        force: when True, MKV/AVI/M4V/MTS/FLV/WebM files are added to
            the mediainfo bucket. When False they are silently skipped.

    Returns:
        A list of 4-tuples ``(subdir, mi_files, r3d_files, wav_files)``
        — one per subdirectory that contained at least one recognised file.
        Each inner list is alphabetically sorted.
    """
    subdirs: list[Path] = []
    for dirpath, dirnames, _ in os.walk(target):
        dirnames.sort()
        subdirs.append(Path(dirpath))

    video_exts = CAMERA_VIDEO_EXTS | (OTHER_VIDEO_EXTS if force else set())

    result: list[tuple[Path, list[Path], list[Path], list[Path]]] = []
    for subdir in subdirs:
        mi: list[Path] = []
        r3d: list[Path] = []
        wav: list[Path] = []
        for f in sorted(subdir.iterdir()):
            if not f.is_file():
                continue
            ext = f.suffix.lower().lstrip(".")
            if ext in video_exts:
                mi.append(f)
            elif ext == "r3d":
                r3d.append(f)
            elif ext == "wav":
                wav.append(f)
        if mi or r3d or wav:
            result.append((subdir, mi, r3d, wav))

    return result


def _batch_groups(
    groups: list[tuple[Path, list[Path], list[Path], list[Path]]],
) -> list[tuple[list[Path], list[Path], list[Path]]]:
    """Merge lone-clip subdirectories into combined batches per file type.

    Some cameras (e.g. ARRI) nest each clip in its own subdirectory, which
    would otherwise cause one subprocess invocation per clip — defeating the
    batching optimisation entirely. This function collapses runs of
    single-clip subdirectories into one batch per file type, so even deeply
    nested structures pay at most one cold-start per file type per run.

    Each file type (mediainfo, R3D, WAV) is tracked independently: a
    subdir with one MOV and one WAV contributes to both buffers separately.
    A subdir with two MOVs flushes the MOV buffer immediately but does not
    affect the WAV buffer.

    The flush condition per type is "this subdir contributes more than one
    file of this type". Any files remaining in a buffer after the final
    subdir are yielded as a last batch, so lone clips at the end of the
    walk are never dropped.

    Args:
        groups: output of ``collect_files_by_subdir``.

    Returns:
        A flat list of ``(mi_files, r3d_files, wav_files)`` batches ready
        for dispatch. The three lists within each batch are homogeneous —
        no cross-type mixing.
    """
    buf_mi: list[Path] = []
    buf_r3d: list[Path] = []
    buf_wav: list[Path] = []
    batches: list[tuple[list[Path], list[Path], list[Path]]] = []

    def _flush() -> None:
        if buf_mi or buf_r3d or buf_wav:
            batches.append((buf_mi[:], buf_r3d[:], buf_wav[:]))
            buf_mi.clear()
            buf_r3d.clear()
            buf_wav.clear()

    for _subdir, mi, r3d, wav in groups:
        buf_mi.extend(mi)
        buf_r3d.extend(r3d)
        buf_wav.extend(wav)
        # Flush when any type has more than one clip from this subdir —
        # that's a normal (non-lone-clip) subdir, dispatch immediately.
        if len(mi) > 1 or len(r3d) > 1 or len(wav) > 1:
            _flush()

    _flush()  # emit any remaining lone-clip accumulations
    return batches


# ---------------------------------------------------------------------------
# Verify system dependencies
# ---------------------------------------------------------------------------


def _check_dependencies() -> None:
    """Verify required system binaries are available on PATH."""
    deps = ["mediainfo", "exiftool"]
    missing = [d for d in deps if shutil.which(d) is None]

    if not missing:
        return

    # Direct errors to stderr to avoid polluting piped output.
    print(f"Error: Missing system dependencies: {', '.join(missing)}", file=sys.stderr)

    if sys.platform == "darwin":
        # Use media-info for Brew CLI installation.
        brew_args = " ".join(["media-info" if d == "mediainfo" else d for d in missing])
        print("Install via Homebrew:", file=sys.stderr)
        print(f"  brew install {brew_args}", file=sys.stderr)

    elif sys.platform.startswith("linux"):
        # Identify package manager and family-specific package names.
        mgr, p_exif = None, "exiftool"

        if shutil.which("apt-get"):
            mgr, p_exif = "apt-get install", "libimage-exiftool-perl"  # Debian-based
        elif shutil.which("yum"):
            mgr, p_exif = "yum install", "perl-Image-ExifTool"  # RHEL-based
        elif shutil.which("zypper"):
            mgr, p_exif = "zypper install", "exiftool"  # SUSE-based
        elif shutil.which("pacman"):
            mgr, p_exif = "pacman -S", "exiftool"  # Arch-based

        if mgr:
            linux_args = " ".join([p_exif if d == "exiftool" else d for d in missing])
            print(f"Install the missing tools via {mgr.split()[0]}:", file=sys.stderr)
            print(f"  {mgr} {linux_args}", file=sys.stderr)
        else:
            # Fallback for Linux distributions without supported managers.
            if "mediainfo" in missing:
                print(
                    "  Download MediaInfo: https://mediaarea.net/en/MediaInfo",
                    file=sys.stderr,
                )
            if "exiftool" in missing:
                print("  Download ExifTool: https://exiftool.org/", file=sys.stderr)

    elif sys.platform == "win32":
        # win32 covers both 32-bit and 64-bit Windows.
        print("Install via winget:", file=sys.stderr)
        print(f"  winget install {' '.join(missing)}", file=sys.stderr)

    else:
        # Final fallback for entirely unsupported platforms.
        if "mediainfo" in missing:
            print(
                "  Download MediaInfo: https://mediaarea.net/en/MediaInfo",
                file=sys.stderr,
            )
        if "exiftool" in missing:
            print("  Download ExifTool: https://exiftool.org/", file=sys.stderr)

    sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: argument list excluding the program name. ``None`` (the
            default) means use ``sys.argv[1:]`` — the normal case.
            Passing an explicit list is useful for testing.

    Returns:
        Process exit code: 0 on success, 1 if the path argument doesn't
        exist or argument parsing failed.
    """
    # On Unix, restore default SIGPIPE handling. By default Python catches
    # SIGPIPE and turns it into BrokenPipeError; restoring SIG_DFL lets
    # the process die quietly when piped into something like ``head``.
    # Windows has no SIGPIPE so we guard with hasattr.
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    p = argparse.ArgumentParser(
        prog="basicmeta",
        description="Basic metadata utility for sanity checking original camera files",
    )
    p.add_argument(
        "path",
        nargs="?",
        default=".",
        help="file or directory to analyze (default: current dir)",
    )

    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="force analysis of non-camera video containers (MKV, AVI, M4V, MTS, FLV, WebM)",
    )
    p.add_argument("--fps", action="store_true", help="print frame rate")
    p.add_argument("--resolution", action="store_true", help="print resolution")
    p.add_argument("--date", action="store_true", help="print encoded date")
    p.add_argument(
        "--sn",
        "--serialnumber",
        dest="sn",
        action="store_true",
        help="print camera serial number (extra exiftool call for non-R3D files)",
    )
    p.add_argument("--version", action="version", version=__version__)
    args = p.parse_args(argv)

    # Build an ordered field list from argv token order.
    # No field flags → all fields in default order.
    _flag_to_field = {
        "--fps": "fps",
        "--resolution": "resolution",
        "--date": "date",
        "--sn": "sn",
        "--serialnumber": "sn",
    }
    # "sn" is absent from _default_fields intentionally — it triggers an
    # extra exiftool call and must only run when explicitly requested.
    _default_fields = ["fps", "resolution", "date"]
    raw_argv = argv if argv is not None else sys.argv[1:]
    fields: list[str] = []
    seen: set[str] = set()
    for token in raw_argv:
        f = _flag_to_field.get(token)
        if f and f not in seen:
            fields.append(f)
            seen.add(f)
    if not fields:
        fields = _default_fields[:]

    target = Path(args.path).resolve()

    # ---- Helpers local to main --------------------------------------------
    def _line(t: tuple, sn_extra: str = "", audio: bool = False) -> str:
        fps_r, res_r, date_r, sn_r, name = t
        # sn_extra overrides the tuple value for non-R3D files (where the
        # serial number comes from a separate batch_sn call, not the tuple).
        return _render_line(fps_r, res_r, date_r, sn_extra or sn_r, name, fields, audio=audio)

    def emit(lines: Iterable[str]) -> None:
        # _render_line always returns a non-empty string (Unknown for missing
        # fields), so nothing is filtered here — every file gets a line.
        for line in lines:
            print(line)

    if target.is_file():
        ext = target.suffix.lower().lstrip(".")
        if ext in CAMERA_VIDEO_EXTS:
            emit(_line(t) for t in batch_mediainfo([target]).values())
        elif ext == "r3d":
            emit(_line(t) for t in batch_r3d([target]).values())
        elif ext == "wav":
            emit(_line(t, audio=True) for t in batch_wav([target]).values())
        elif ext in OTHER_VIDEO_EXTS and args.force:
            emit(_line(t) for t in batch_mediainfo([target]).values())
        return 0

    # ---- Error: bad path ---------------------------------------------------
    if not target.is_dir():
        print(f"Error: '{target}' is not a valid file or directory.", file=sys.stderr)
        return 1

    # ---- Directory mode ----------------------------------------------------
    # Files are batched by _batch_groups, which merges lone-clip subdirs so
    # that cameras nesting one clip per folder don't pay a cold-start per
    # clip. Results are printed progressively as each batch completes.
    UNDERLINE = "\033[4m" if _ANSI_OK else ""

    # Build the header using the same anchor logic as _render_line so that
    # label columns align exactly with data columns. We use plain label
    # strings (no ANSI colour inside the value) and space-padding instead
    # of hyphens between columns.
    _field_labels = {
        "fps": "FPS",
        "resolution": "RESOLUTION",
        "date": "DATE",
        "sn": "S/N",
    }

    # Map each label to the visible width its data column uses, mirroring
    # _render_line: fps appends " fps" so its slot = len("FPS") padded to
    # match the widest fps value ("23.976 fps" = 10); resolution and date
    # columns are variable-width in data rows so we use the widest expected
    # value to set a minimum label width.
    # The header is produced by calling _render_line with plain label strings
    # mapped to the fps/res/date/sn slots — but _render_line adds " fps" to
    # the fps value, so we pass the label without that suffix and let the
    # function append it.
    #
    # Simplest correct approach: build the header string directly using the
    # same anchor positions as _render_line (_COL_DATE, _COL_FILE) with
    # space-padding (not hyphens) between labels.
    active_labels = [(f, _field_labels[f]) for f in fields if f in _field_labels]
    if active_labels:
        # Compute visible widths matching _render_line's field rendering:
        # fps -> "FPS fps" (but label is just "FPS", data appends " fps")
        # To mirror data alignment, treat label widths as data widths by
        # using widest-possible data values for column sizing.
        # Rather than duplicating the anchor math, reuse _render_line with
        # synthetic plain-text label values:
        #   fps field  -> "FPS" (rendered as "FPS fps" in the line)
        #   res field  -> "RESOLUTION"
        #   date field -> "DATE"
        #   sn field   -> "S/N"
        # We need to fake audio=False (video header) so res shows normally.
        # _render_line appends " fps" to the fps value; strip that from label
        # by temporarily monkey-patching... actually just build directly:
        # The header is a plain string at the same column positions as data.
        # We build it like _render_line but with plain labels and spaces.
        hdr_parts: list[tuple[str, int, str]] = []
        for f, label in active_labels:
            if f in {"fps", "resolution", "date"}:
                display = label
            else:
                display = label
            hdr_parts.append((display, len(display), f))

        # Mirror _render_line's anchor logic with space separators.
        n = len(hdr_parts)
        anchor_idx = next((i for i, (_, _, f) in enumerate(hdr_parts) if f == "date"), n - 1)
        anchor_col = _COL_DATE if hdr_parts[anchor_idx][2] == "date" else _COL_FILE - 2 - hdr_parts[anchor_idx][1]
        left_w = sum(w for _, w, _ in hdr_parts[:anchor_idx])
        n_gaps = anchor_idx
        space_budget = anchor_col - left_w  # total spaces for all gaps
        if n_gaps > 0:
            # Distribute spaces evenly (headers use spaces, not hyphens).
            base_sp, rem_sp = divmod(space_budget, n_gaps)
            sp = [base_sp + (1 if i < rem_sp else 0) for i in range(n_gaps)]
        else:
            sp = []

        hdr_line = ""
        for i in range(anchor_idx + 1):
            if i > 0:
                hdr_line += " " * sp[i - 1]
            d, _, f = hdr_parts[i]
            hdr_line += f"{DIM}{UNDERLINE}{d}{RESET}"
        for i in range(anchor_idx + 1, n):
            hdr_line += "  " + f"{DIM}{UNDERLINE}{hdr_parts[i][0]}{RESET}"
        # Pad to _COL_FILE - 2 then append (FILENAME).
        vis_len = _vis(hdr_line)
        hdr_line += " " * max(2, _COL_FILE - 2 - vis_len)
        hdr_line += f"{DIM}{UNDERLINE}(FILENAME){RESET}"

    all_raw: list[tuple[str, str, str, str]] = []  # (fps, res, date, sn)

    groups = collect_files_by_subdir(target, args.force)
    batches = _batch_groups(groups)
    if not batches:
        return 0

    if active_labels:
        print(hdr_line)

    for mi, r3d, wav in batches:
        mi_out = batch_mediainfo(mi)
        r3d_out = batch_r3d(r3d)
        wav_out = batch_wav(wav)
        # batch_sn is only called when --sn was requested; for R3D the
        # serial number is already inside the batch_r3d tuple.
        sn_mi = batch_sn(mi) if "sn" in fields else {}
        sn_wav = batch_sn(wav) if "sn" in fields else {}

        lines: list[str] = []
        for p in mi:
            if p in mi_out:
                t = mi_out[p]
                sn = sn_mi.get(p, "")
                lines.append(_line(t, sn_extra=sn))
                all_raw.append((t[0], t[1], t[2], sn))
        for p in r3d:
            if p in r3d_out:
                t = r3d_out[p]
                lines.append(_line(t))
                all_raw.append((t[0], t[1], t[2], t[3]))
        for p in wav:
            if p in wav_out:
                t = wav_out[p]
                sn = sn_wav.get(p, "")
                lines.append(_line(t, sn_extra=sn, audio=True))
                all_raw.append((t[0], t[1], t[2], sn))
        emit(lines)

    # ---- Summary -------------------------------------------------------------
    fps_vals = {r[0] for r in all_raw if r[0]} if "fps" in fields else set()
    res_vals = {r[1] for r in all_raw if r[1] and r[1] != "Audio"} if "resolution" in fields else set()
    date_vals = {r[2] for r in all_raw if r[2]} if "date" in fields else set()
    sn_vals = {r[3] for r in all_raw if r[3]} if "sn" in fields else set()

    if all_raw and len(set(all_raw)) == 1:
        print("\n🎯 All the files scanned have the same frame rate, resolution and encoded date")
    elif all_raw:
        mismatched: list[str] = []
        if len(fps_vals) > 1:
            mismatched.append("frame rate")
        if len(res_vals) > 1:
            mismatched.append("resolution")
        if len(date_vals) > 1:
            mismatched.append("encoded date")
        if len(sn_vals) > 1:
            mismatched.append("serial number")

        if mismatched:
            summary_fields = (
                mismatched[0] if len(mismatched) == 1 else ", ".join(mismatched[:-1]) + f" and {mismatched[-1]}"
            )
            print(f"\n\U0001f440 Manual check required, some files have a different {summary_fields}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
