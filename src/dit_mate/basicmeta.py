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
import importlib.metadata
import json
import os
import re
import shutil
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

from dit_mate._internal import term
from dit_mate._internal.binaries import run_capture
from dit_mate._internal.utils import FieldOrderAction

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

#: One parsed media row: ``(fps, resolution, date, serial_number, name)``.
#: Returned per file by the ``batch_*`` functions and consumed by ``_format_line``.
_MetaRow = tuple[str, str, str, str, str]

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

# Emit colour only when stdout is a TTY and ANSI is supported; piping to a
# file or another process drops colour automatically. Codes come from the
# shared palette so every tool renders the same hues.
_COLOR = term.supports_color(sys.stdout)
DIM = term.DIM if _COLOR else ""  # filename suffix
RED = term.RED if _COLOR else ""  # missing / unknown fields
ORANGE = term.ORANGE if _COLOR else ""  # VFR / non-standard fps, sub-HD res
UNDERLINE = term.UNDERLINE if _COLOR else ""  # column headers
RESET = term.RESET if _COLOR else ""

#: Broadcast-standard frame rates. fps strings not in this set are
#: coloured orange as a heads-up that the rate is unusual. Entries must be
#: in _norm_fps's canonical form: trailing zeros stripped.
KNOWN_FPS = {
    "23.976",
    "23.97",
    "24",
    "25",
    "29.97",
    "30",
    "50",
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

#: Visible (ANSI-stripped) string length — colour codes take no screen space.
_vis = term.visible_len


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
# Field normalization
# ---------------------------------------------------------------------------


def _norm_fps(s: str) -> str:
    """Normalize a frame-rate value to the display form used by basicmeta.

    Parses any numeric input, formats with three decimal places, then strips
    trailing zeros (and a dangling ``.``) so every rate collapses to one
    canonical form. Non-numeric input is returned unchanged (apart from
    leading/trailing whitespace). KNOWN_FPS entries must use these same
    shortest forms.

    Args:
        s: raw frame rate as reported by mediainfo or exiftool.

    Returns:
        ``""`` if the input was empty/whitespace, otherwise a clean
        display string like ``"23.976"``, ``"24"``, or ``"29.97"``.
    """
    s = (s or "").strip()
    if not s:
        return ""
    try:
        # Format with 3 decimals, then strip trailing zeros.
        f = float(s)
        return f"{f:.3f}".rstrip("0").rstrip(".")
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


def batch_mediainfo(paths: list[Path]) -> dict[Path, _MetaRow]:
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
        A dict mapping each input path to a ``_MetaRow`` 5-tuple
        ``(fps, res, date, sn, name)`` — all plain strings for the caller
        to render with the active field list (``sn`` is always ``""``
        here; mediainfo doesn't report serial numbers).
    """
    if not paths:
        return {}

    raw = run_capture(["mediainfo", "--Output=JSON", *map(str, paths)])
    results: dict[Path, _MetaRow] = {}

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

        # FileNameExtension is the bare filename (e.g. "clip_001.mov"), not
        # just the extension — confusing but matches the original tool.
        ext_name = gen.get("FileNameExtension") or p.name

        # Plain strings only — colour is applied at render time by
        # _build_field_cells, and the consistency summary compares these
        # values across handlers.
        results[p] = (fps_raw, res, date, "", ext_name or p.name)

    return results


@dataclass(frozen=True)
class _FieldValues:
    """Raw per-field values for one media file, before colouring/layout.

    ``fps``/``res``/``date`` are plain strings (e.g. ``"23.976"``,
    ``"3840 x 2160"``, ``"2024-06-15"``); ``sn`` is the camera serial
    number. Any field may be ``""`` when absent.
    """

    fps: str = ""
    res: str = ""
    date: str = ""
    sn: str = ""


def _build_field_cells(vals: _FieldValues, fields: list[str], *, audio: bool) -> list[tuple[str, int, str]]:
    """Build ``(coloured_value, visible_width, field_name)`` for each active field.

    Colour is applied here (orange fps/low-res, red Unknown); ``audio``
    renders the ``"resolution"`` field as ``"Audio"``. Unknown field keys
    are skipped.
    """
    cells: list[tuple[str, int, str]] = []
    for field in fields:
        if field == "fps":
            if vals.fps:
                c = ORANGE if vals.fps not in KNOWN_FPS else ""
                val = f"{c}{vals.fps}{RESET if c else ''} fps"
            else:
                val = f"{RED}Unknown{RESET} fps"
        elif field == "resolution":
            if audio:
                val = "Audio"
            elif vals.res:
                c = ORANGE if _res_is_low(vals.res) else ""
                val = f"{c}{vals.res}{RESET if c else ''}"
            else:
                val = f"{RED}Unknown{RESET}"
        elif field == "date":
            val = vals.date or f"{RED}Unknown{RESET}"
        elif field == "sn":
            val = vals.sn or f"{RED}Unknown{RESET}"
        else:
            continue
        cells.append((val, _vis(val), field))
    return cells


def _render_line(vals: _FieldValues, name: str, fields: list[str], *, audio: bool = False) -> str:
    """Assemble one output line from raw field values.

    Columns are anchored by fixed visible-column positions: DATE always starts
    at ``_COL_DATE`` (col 27) and the filename always starts at ``_COL_FILE``
    (col 39). The separators between fields are hyphen runs of the form
    " ----- " whose lengths are computed dynamically so that these anchors
    are always hit, regardless of which fields are active or how wide their
    values are. ANSI colour codes are stripped before measuring widths so
    that coloured values (orange fps, red Unknown) align correctly.

    Args:
        vals:   raw per-field values (fps/res/date/sn).
        name:   filename shown in the trailing dim parens.
        fields: ordered list of field keys to include
                (``"fps"``, ``"resolution"``, ``"date"``, ``"sn"``).
        audio:  when True ``"resolution"`` renders as ``"Audio"``.

    Returns:
        The formatted single-line string ready for printing.
    """
    coloured = _build_field_cells(vals, fields, audio=audio)
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


def batch_r3d(paths: list[Path]) -> dict[Path, _MetaRow]:
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
        A dict mapping each input path to a ``_MetaRow`` 5-tuple
        ``(fps, res, date, sn, name)`` for rendering and consistency
        checking.
    """
    if not paths:
        return {}

    out = run_capture(
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

    results: dict[Path, _MetaRow] = {}
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


def _first_fps(rec: dict[str, str], keys: tuple[str, ...]) -> str:
    """Return the first key's value normalised to an fps string, or ``""``."""
    for k in keys:
        v = rec.get(k, "")
        if v:
            fps = _norm_fps(v)
            if fps:
                return fps
    return ""


def _first_date(rec: dict[str, str], keys: tuple[str, ...]) -> str:
    """Return the first key holding a real (non-zero) date, normalised to ISO, or ``""``.

    The non-zero-digit check filters out ``0000-00-00`` placeholders that
    some recorders write when no system clock is set, without paying for a
    full parse on every candidate.
    """
    for k in keys:
        v = rec.get(k, "")
        if v and HAS_NONZERO_DIGIT.search(v):
            d = _norm_date_iso(v)
            if d:
                return d
    return ""


def batch_wav(paths: list[Path]) -> dict[Path, _MetaRow]:
    """Process all WAV files in a single exiftool call.

    Production audio WAVs (BWF — Broadcast Wave Format) carry timecode
    and date info in iXML or BEXT chunks. Different recorders write the
    same logical fields under different tags, so we probe several aliases
    for both the frame rate (timecode rate) and the date, taking the
    first one that has a usable value.

    Args:
        paths: WAV files to analyze. May be empty.

    Returns:
        A dict mapping each input path to a ``_MetaRow`` 5-tuple
        ``(fps, "Audio", date, "", name)`` for rendering and consistency
        checking.
    """
    if not paths:
        return {}

    out = run_capture(
        [
            "exiftool",
            "-s",
            "-s",
            # Frame-rate aliases, in order of preference. The first non-empty
            # value wins. iXML's BWF tags are the most authoritative for
            # location-sound recordings.
            "-BwfxmlSpeedTimecodeRate",
            "-Speed",
            "-VideoFrameRate",
            # Date aliases. We probe all of them and take the first that
            # contains real (non-zero) digits, since some recorders write
            # placeholder dates when no system clock is set.
            "-DateTimeOriginal",
            "-DateCreated",
            "-OriginatorReference",
            "-BwfxmlBextBwfOriginationDate",
            "-Filename",
            *map(str, paths),
        ]
    )
    recs = _split_exiftool_batches(out, len(paths))

    # Probe order matters: try the most-specific tags first so a recorder
    # that writes both BWF timecode rate and a generic VideoFrameRate ends
    # up with the BWF value (which is the intended audio reference).
    fps_keys = ("BwfxmlSpeedTimecodeRate", "VideoFrameRate", "Speed")
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

    results: dict[Path, _MetaRow] = {}
    for p in paths:
        rec = by_file.get(os.path.normpath(str(p)))
        if rec is None and len(paths) == 1 and recs:
            rec = recs[0]
        if rec is None:
            rec = {}

        # First non-empty FPS-equivalent tag wins; first date tag with a
        # real (non-zero) value wins. Probe order is significant (see above).
        fps = _first_fps(rec, fps_keys)
        date = _first_date(rec, date_keys)

        name = rec.get("FileName") or rec.get("Filename") or p.name
        results[p] = (fps, "Audio", date, "", name)
    return results


# ---------------------------------------------------------------------------
# Serial-number lookup (separate on-demand exiftool call)
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

    out = run_capture(
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


#: Manual-download URLs printed when no package manager is available.
_DOWNLOAD_HINTS = {
    "mediainfo": "  Download MediaInfo: https://mediaarea.net/en/MediaInfo",
    "exiftool": "  Download ExifTool: https://exiftool.org/",
}

#: Linux package managers in detection order:
#: (probe binary, install command, distro-specific exiftool package name).
_LINUX_PKG_MANAGERS = (
    ("apt-get", "apt-get install", "libimage-exiftool-perl"),  # Debian-based
    ("yum", "yum install", "perl-Image-ExifTool"),  # RHEL-based
    ("zypper", "zypper install", "exiftool"),  # SUSE-based
    ("pacman", "pacman -S", "exiftool"),  # Arch-based
)


def _print_download_links(missing: list[str]) -> None:
    """Print manual download URLs for any missing deps (no-package-manager fallback)."""
    for dep in missing:
        if dep in _DOWNLOAD_HINTS:
            print(_DOWNLOAD_HINTS[dep], file=sys.stderr)


def _linux_install_help(missing: list[str]) -> None:
    """Print a distro-appropriate install command, falling back to download links."""
    for probe, cmd, p_exif in _LINUX_PKG_MANAGERS:
        if shutil.which(probe):
            args = " ".join(p_exif if d == "exiftool" else d for d in missing)
            print(f"Install the missing tools via {cmd.split()[0]}:", file=sys.stderr)
            print(f"  {cmd} {args}", file=sys.stderr)
            return
    _print_download_links(missing)


def _print_install_help(missing: list[str]) -> None:
    """Print platform-appropriate install instructions to stderr."""
    if sys.platform == "darwin":
        # Homebrew names the MediaInfo CLI "media-info".
        brew_args = " ".join("media-info" if d == "mediainfo" else d for d in missing)
        print("Install via Homebrew:", file=sys.stderr)
        print(f"  brew install {brew_args}", file=sys.stderr)
    elif sys.platform.startswith("linux"):
        _linux_install_help(missing)
    elif sys.platform == "win32":
        # win32 covers both 32-bit and 64-bit Windows.
        print("Install via winget:", file=sys.stderr)
        print(f"  winget install {' '.join(missing)}", file=sys.stderr)
    else:
        # Entirely unsupported platform — point at the download pages.
        _print_download_links(missing)


def _check_dependencies() -> None:
    """Verify required system binaries are available on PATH."""
    deps = ["mediainfo", "exiftool"]
    missing = [d for d in deps if shutil.which(d) is None]
    if not missing:
        return
    # Direct errors to stderr to avoid polluting piped output.
    print(f"Error: Missing system dependencies: {', '.join(missing)}", file=sys.stderr)
    _print_install_help(missing)
    sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# "sn" is intentionally excluded from the default set — it triggers an extra
# exiftool call and must only run when explicitly requested.
_DEFAULT_FIELDS = ["fps", "resolution", "date"]
_FIELD_LABELS = {
    "fps": "FPS",
    "resolution": "RESOLUTION",
    "date": "DATE",
    "sn": "S/N",
}


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the basicmeta CLI."""
    p = argparse.ArgumentParser(
        prog="basicmeta",
        description="Basic metadata utility for sanity checking original camera files",
    )
    p.set_defaults(field_order=[])
    p.add_argument("path", nargs="?", default=".", help="file or directory to analyze (default: current dir)")
    p.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="force analysis of non-camera video containers (MKV, AVI, M4V, MTS, FLV, WebM)",
    )
    p.add_argument("--fps", action=FieldOrderAction, field="fps", help="print frame rate")
    p.add_argument("--resolution", action=FieldOrderAction, field="resolution", help="print resolution")
    p.add_argument("--date", action=FieldOrderAction, field="date", help="print encoded date")
    p.add_argument(
        "--sn",
        "--serialnumber",
        dest="sn",
        action=FieldOrderAction,
        field="sn",
        help="print camera serial number (extra exiftool call for non-R3D files)",
    )
    p.add_argument("--version", action="version", version=__version__)
    return p


def _format_line(t: _MetaRow, fields: list[str], *, sn_extra: str = "", audio: bool = False) -> str:
    """Format one output line from a batch result tuple ``(fps, res, date, sn, name)``.

    ``sn_extra`` overrides the tuple's serial number for non-R3D files
    (where the serial comes from a separate batch_sn call, not the tuple).
    """
    fps_r, res_r, date_r, sn_r, name = t
    vals = _FieldValues(fps=fps_r, res=res_r, date=date_r, sn=sn_extra or sn_r)
    return _render_line(vals, name, fields, audio=audio)


def _render_single_file(target: Path, fields: list[str], *, force: bool) -> int:
    """Print metadata line(s) for a single file argument; return exit code."""
    ext = target.suffix.lower().lstrip(".")
    if ext in CAMERA_VIDEO_EXTS or (ext in OTHER_VIDEO_EXTS and force):
        results = batch_mediainfo([target])
    elif ext == "r3d":
        results = batch_r3d([target])
    elif ext == "wav":
        results = batch_wav([target])
    elif ext in OTHER_VIDEO_EXTS:
        print(f"Skipped non-camera video container: {target.name} (use -f to analyze it)", file=sys.stderr)
        return 1
    else:
        print(f"Unsupported file type: {target.name}", file=sys.stderr)
        return 1
    for t in results.values():
        print(_format_line(t, fields, audio=ext == "wav"))
    return 0


def _build_header(active_labels: list[tuple[str, str]]) -> str:
    """Build the column header line, anchored exactly like ``_render_line``.

    Uses plain label strings with space separators (not hyphens) so header
    columns land at the same fixed positions (``_COL_DATE``, ``_COL_FILE``)
    as the data rows.
    """
    underline = UNDERLINE
    hdr_parts = [(label, len(label), f) for f, label in active_labels]

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
        hdr_line += f"{DIM}{underline}{hdr_parts[i][0]}{RESET}"
    for i in range(anchor_idx + 1, n):
        hdr_line += "  " + f"{DIM}{underline}{hdr_parts[i][0]}{RESET}"
    # Pad to _COL_FILE - 2 then append (FILENAME).
    vis_len = _vis(hdr_line)
    hdr_line += " " * max(2, _COL_FILE - 2 - vis_len)
    hdr_line += f"{DIM}{underline}(FILENAME){RESET}"
    return hdr_line


def _process_batch(
    mi: list[Path],
    r3d: list[Path],
    wav: list[Path],
    fields: list[str],
) -> tuple[list[str], list[tuple[str, str, str, str]]]:
    """Run the metadata tools for one batch; return (output lines, raw field tuples).

    ``batch_sn`` is only called when ``--sn`` was requested; for R3D the
    serial number is already inside the ``batch_r3d`` tuple.
    """
    mi_out = batch_mediainfo(mi)
    r3d_out = batch_r3d(r3d)
    wav_out = batch_wav(wav)
    sn_mi = batch_sn(mi) if "sn" in fields else {}
    sn_wav = batch_sn(wav) if "sn" in fields else {}

    lines: list[str] = []
    raw: list[tuple[str, str, str, str]] = []
    for p in mi:
        if p in mi_out:
            t = mi_out[p]
            sn = sn_mi.get(p, "")
            lines.append(_format_line(t, fields, sn_extra=sn))
            raw.append((t[0], t[1], t[2], sn))
    for p in r3d:
        if p in r3d_out:
            t = r3d_out[p]
            lines.append(_format_line(t, fields))
            raw.append((t[0], t[1], t[2], t[3]))
    for p in wav:
        if p in wav_out:
            t = wav_out[p]
            sn = sn_wav.get(p, "")
            lines.append(_format_line(t, fields, sn_extra=sn, audio=True))
            raw.append((t[0], t[1], t[2], sn))
    return lines, raw


def _emit_directory(target: Path, fields: list[str], *, force: bool) -> list[tuple[str, str, str, str]]:
    """Walk a directory, print the header + one line per file, return raw field tuples.

    Files are batched by ``_batch_groups``, which merges lone-clip subdirs so
    cameras nesting one clip per folder don't pay a cold-start per clip.
    Results print progressively as each batch completes.
    """
    active_labels = [(f, _FIELD_LABELS[f]) for f in fields if f in _FIELD_LABELS]

    groups = collect_files_by_subdir(target, force)
    batches = _batch_groups(groups)
    all_raw: list[tuple[str, str, str, str]] = []
    if not batches:
        return all_raw

    if active_labels:
        print(_build_header(active_labels))

    for mi, r3d, wav in batches:
        lines, raw = _process_batch(mi, r3d, wav, fields)
        all_raw.extend(raw)
        for line in lines:
            print(line)
    return all_raw


def _print_summary(all_raw: list[tuple[str, str, str, str]], fields: list[str]) -> None:
    """Print the cross-file consistency summary."""
    if not all_raw:
        return
    if len(set(all_raw)) == 1:
        print("\n🎯 All the files scanned have the same frame rate, resolution and encoded date")
        return

    # (field key, tuple index, human label, value to skip when collecting).
    checks = (
        ("fps", 0, "frame rate", None),
        ("resolution", 1, "resolution", "Audio"),
        ("date", 2, "encoded date", None),
        ("sn", 3, "serial number", None),
    )
    mismatched = [
        label
        for key, idx, label, skip in checks
        if key in fields and len({r[idx] for r in all_raw if r[idx] and r[idx] != skip}) > 1
    ]
    if not mismatched:
        return
    summary_fields = mismatched[0] if len(mismatched) == 1 else ", ".join(mismatched[:-1]) + f" and {mismatched[-1]}"
    print(f"\n\U0001f440 Manual check required, some files have a different {summary_fields}")


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

    args = _build_parser().parse_args(argv)
    fields = args.field_order or _DEFAULT_FIELDS[:]
    target = Path(args.path).resolve()

    if target.is_file():
        return _render_single_file(target, fields, force=args.force)

    if not target.is_dir():
        print(f"Error: '{target}' is not a valid file or directory.", file=sys.stderr)
        return 1

    all_raw = _emit_directory(target, fields, force=args.force)
    _print_summary(all_raw, fields)
    return 0


if __name__ == "__main__":
    sys.exit(main())
