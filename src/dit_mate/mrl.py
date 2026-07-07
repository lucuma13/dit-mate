#!/usr/bin/env python3
"""mrl — Master Rushes Log helper for DITs.

Scans a camera-roll directory (or several) and emits the values you need to
paste into a Master Rushes Log: such as first/last clip name, total size in GB,
aggregated video duration, and clip count.

Handles the directory layouts that common camera copy tools produce:

- Sony XAVC / FX9:   ``<ROLL>/XDROOT/Clip/<CLIPNAME>.MXF``
- DJI ProRes:        ``<ROLL>/<CLIPDIR>/<CLIPNAME>.MOV``
- RED R3D:           ``<ROLL>/<RDM>/<CLIPNAME>.RDC/<CLIPNAME>_NNN.R3D``
- RED ProRes/H.265:  ``<ROLL>/<RDM>/<CLIPNAME>.RDC/<CLIPNAME>.mov`` (Komodo/DSMC3)
- GoPro:             ``<ROLL>/<CLIPNAME>.MP4`` + ``.LRV`` proxy + ``.WAV``
- Insta360:          ``<ROLL>/DCIM/Camera01/<CLIPNAME>.insv``
- Sound mixer WAVs:  ``<ROLL>/<CLIPNAME>.wav``

Multi-roll detection
--------------------
If you point ``mrl`` at a directory that contains multiple rolls, each roll
is emitted on its own line with the roll name dimmed in parentheses at the
end.

With a single path (or no path, defaulting to cwd), ``mrl`` scans for
known parent directories (CAMERA, AUDIO, VENICE, 4D, etc.) one or two
levels deep. If found, it treats their subdirectories as rolls. If not
found, the given directory itself is the roll. Known siblings (MEZZ,
MEZZANINE, and any directory containing PROXY or PROXIES) are always
excluded from roll discovery.

With multiple paths, each path is treated as a named roll directly — no
substructure detection. Pass them in the order you want them reported.

Combined short flags work POSIX-style: ``-flsdc`` is equivalent to
``-f -l -s -d -c``. Column order in the output follows the order flags
are given on the command line.
"""

from __future__ import annotations

import argparse
import functools
import importlib.metadata
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyperclip

from dit_mate._internal import term
from dit_mate._internal.binaries import invoke, run_capture
from dit_mate._internal.config import CONFIG_DIR, bundled_data, load_or_seed_toml
from dit_mate._internal.openers import maybe_open_config
from dit_mate._internal.utils import FieldOrderAction

# -----------------------------------------------------------------------------
# Version
# -----------------------------------------------------------------------------

try:
    __version__ = importlib.metadata.version("dit-mate")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"

# ---------------------------------------------------------------------------
# Roll-detection config  (loaded from mrl_presets.toml in user config dir)
# ---------------------------------------------------------------------------

PRESETS_FILENAME = "mrl_presets.toml"
DEFAULT_PRESETS = "mrl_default_presets.toml"
PRESETS_PATH = CONFIG_DIR / PRESETS_FILENAME

_REQUIRED_KEYS = {
    "known_parents",
    "known_parents_flex",
    "known_parents_cam_prefix",
    "known_parents_cam_suffix",
    "known_siblings_exact",
    "known_siblings_contains",
}


def _validate_roll_detection(cfg: dict) -> None:
    """Validate required keys and field types in the [roll_detection] table.

    Exits with a config-pointed message on the first problem found.
    """
    missing = _REQUIRED_KEYS - set(cfg.keys())
    if missing:
        sys.exit(
            f"❌  [roll_detection] in {PRESETS_PATH} is missing required keys:\n"
            f"    {', '.join(sorted(missing))}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )
    for list_key in ("known_parents", "known_parents_flex", "known_siblings_exact", "known_siblings_contains"):
        if not isinstance(cfg[list_key], list):
            sys.exit(
                f"❌  [roll_detection].{list_key} must be a list of quoted strings.\n"
                f"    Run 'mrl -E' to edit the file and fix the error."
            )
    for str_key in ("known_parents_cam_prefix", "known_parents_cam_suffix"):
        if not isinstance(cfg[str_key], str):
            sys.exit(
                f"❌  [roll_detection].{str_key} must be a quoted string (regex pattern).\n"
                f"    Run 'mrl -E' to edit the file and fix the error."
            )


def _compile_roll_patterns(cfg: dict) -> tuple[list[re.Pattern], re.Pattern, re.Pattern]:
    """Compile the regex fields, exiting with a config-pointed error on bad patterns.

    Catching bad patterns here means the error points at the config file
    rather than crashing later inside the roll-detection logic.
    """
    try:
        flex = [re.compile(pat, re.IGNORECASE) for pat in cfg["known_parents_flex"]]
    except re.error as exc:
        sys.exit(
            f"❌  Bad regex in [roll_detection].known_parents_flex: {exc}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )
    try:
        prefix = re.compile(cfg["known_parents_cam_prefix"], re.IGNORECASE)
        suffix = re.compile(cfg["known_parents_cam_suffix"], re.IGNORECASE)
    except re.error as exc:
        sys.exit(
            f"❌  Bad regex in [roll_detection] cam prefix/suffix: {exc}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )
    return flex, prefix, suffix


def _load_roll_detection_config() -> dict:
    """Load and validate the [roll_detection] table from mrl_presets.toml.

    On first run the file does not exist; it is seeded from the bundled
    mrl_default_presets.toml that ships with the package, then read back.
    On every subsequent run the user's live copy is used instead, so
    customisations (extra camera names, site-specific sibling patterns)
    survive package updates.
    """
    data = load_or_seed_toml(PRESETS_PATH, bundled_data(DEFAULT_PRESETS))

    if "roll_detection" not in data:
        sys.exit(
            f"❌  Missing [roll_detection] table in {PRESETS_PATH}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )

    cfg = data["roll_detection"]
    _validate_roll_detection(cfg)
    compiled_flex, compiled_cam_prefix, compiled_cam_suffix = _compile_roll_patterns(cfg)

    return {
        "kp_exact": frozenset(s.lower() for s in cfg["known_parents"]),
        "kp_flex": compiled_flex,
        "kp_cam_prefix": compiled_cam_prefix,
        "kp_cam_suffix": compiled_cam_suffix,
        "ks_exact": frozenset(s.lower() for s in cfg["known_siblings_exact"]),
        "ks_contains": frozenset(s.lower() for s in cfg["known_siblings_contains"]),
    }


# Loaded lazily on first use (see _rd) rather than at import time, so that
# -E/-O stay usable when the config file is invalid and --help/--version
# never touch (or seed) the config at all.
@functools.cache
def _rd() -> dict:
    """Return the roll-detection config, loading and caching it on first use."""
    return _load_roll_detection_config()


# ---------------------------------------------------------------------------
# Professional acquisition formats.
# .R3D is intentionally absent here — R3D files are inside RDC directories
# and we surface RDC-as-clip via the discovery layer instead of treating
# raw R3D files as clips.
VIDEO_EXTS = {".mxf", ".mov", ".mp4", ".insv"}

# RED-specific: directory extension that wraps the R3D chunks of one clip.
RDC_DIR_EXT = ".rdc"
R3D_FILE_EXT = ".r3d"

# RED sidecars that live *inside* an RDC alongside the R3D chunks. We don't
# count them, but if any of these turn up *outside* an RDC it means a copy
# or move went wrong — flag for manual review.
RED_SIDECAR_EXTS = {".rmd", ".rlx", ".rsx"}

# Insta360 camera.
# .insv is the primary video clip (lives in DCIM/Camera01/).
# .lrv is a low-resolution proxy generated alongside every .insv by the
# camera itself — analogous to GoPro's .LRV proxy files. We recognise the
# extension so we can silently ignore these sidecars rather than treating
# them as extra clips or raising a warning.
INSV_EXT = ".insv"
INSV_LRV_EXT = ".lrv"

# GoPro chaptered recordings. When a take exceeds the FAT32 4 GB limit
# (or the 12 GB ceiling on newer cards), the camera splits it across
# multiple files following the pattern ``[GH|GX|GP][zz][xxxx].MP4``:
#   - GH = AVC encoding, GX = HEVC, GP = older format
#   - zz (chapter, 01-99) increments per chunk of the same recording
#   - xxxx (file/take number) stays the same across chunks of one take
#     and increments when a new recording starts
# So GX010001 + GX020001 + GX030001 are three chunks of *one* take, while
# GX010002 is a separate take. Sorted alphabetically, chunks of the same
# take aren't adjacent (`GX010001, GX010002, GX020001`), so we have to
# detect chaptering and group chunks back together to get correct clip
# counts and endpoints.
_GOPRO_CHAPTER_RE = re.compile(r"^G([HXP])(\d{2})(\d{4})$", re.IGNORECASE)

# Audio formats. WAV is the standard BWF output of most field recorders;
# ZAX is Zaxcom's proprietary MARF container.
# MIC is Sound Devices' proprietary container.
AUDIO_EXTS = {".wav", ".zax", ".mic"}
ZAX_EXT = ".zax"
MIC_EXT = ".mic"

# Cross-device contamination is detected by reducing each video clip
# name to a "session signature" and requiring all video clips on a
# roll to share the same signature. The signature is the sequence of
# letter-runs in the part of the filename *before the first underscore*.
_LETTER_RUN_RE = re.compile(r"[A-Za-z]+")


def _session_signature(name: str) -> tuple[str, ...]:
    """Return a roll-session signature for one clip name.

    The signature is the sequence of letter-runs (lowercased) in the
    portion of ``name`` before the first underscore. Two clips from
    the same camera session always have identical signatures; a
    mismatch indicates the clips come from different recording sessions
    that ended up on the same roll directory.

    Args:
        name: clip name without extension. For RED clips this is the
            ``.RDC`` directory's basename minus the extension; for
            GoPro chaptered groups this is the canonical chapter-01
            stem; for everything else it's the file's stem.

    Returns:
        Tuple of lowercased letter-runs from the pre-underscore prefix.
        Empty tuple if the prefix has no letters at all (e.g. all-digit
        names, which match other all-digit names).
    """
    prefix = name.split("_", 1)[0]
    return tuple(m.group(0).lower() for m in _LETTER_RUN_RE.finditer(prefix))


# ---------------------------------------------------------------------------
# Terminal colors (TTY only — disabled when piping to a file)
# ---------------------------------------------------------------------------

# Codes come from the shared palette so every tool renders the same hues.
# stdout and stderr are gated independently (the header prints to stderr).
_COLOR = term.supports_color(sys.stdout)
_COLOR_ERR = term.supports_color(sys.stderr)
DIM = term.DIM if _COLOR else ""
RESET = term.RESET if _COLOR else ""
DIM_ERR = term.DIM if _COLOR_ERR else ""
UNDERLINE_ERR = term.UNDERLINE if _COLOR_ERR else ""
RESET_ERR = term.RESET if _COLOR_ERR else ""

# Windows Explorer reports sizes in GiB (1 GiB = 2^30 bytes) while
# macOS Finder and Linux tools report in decimal GB (1 GB = 10^9 bytes).
# We match the convention of the host OS so the numbers agree with what
# the user sees in their file manager.
_SIZE_IS_GIB = os.name == "nt"
_SIZE_DIVISOR = 1_073_741_824 if _SIZE_IS_GIB else 1_000_000_000
_SIZE_UNIT = "GiB" if _SIZE_IS_GIB else "GB"


# ---------------------------------------------------------------------------
# Clip abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clip:
    """One logical clip on the rushes log.

    A clip is either a single media file (the common case — MXF, MOV,
    MP4, WAV all map 1:1) or a RED ``.RDC`` directory whose ``.R3D``
    children are sequential chunks of one take.

    Attributes:
        name: Clip name as it appears in the Master Rushes Log, without
            extension. For files this is ``path.stem``. For RDCs it's
            the directory's basename minus ``.RDC``.
        files: All concrete files that contribute size and duration to
            this clip. For a single-file clip it's a one-element tuple.
            For an RDC it's all the R3D chunks (sidecar RMD/RLX/RSX
            files are excluded). Always non-empty.
        anchor: A representative path used for grouping into rolls and
            for sort-order purposes. For a single-file clip, the file
            itself; for an RDC, the RDC directory.
        kind: ``"video"`` for video clips (single MXF/MOV/MP4, or any
            RDC), ``"audio"`` for WAV. Drives the first/last selection
            rules.
    """

    name: str
    files: tuple[Path, ...]
    anchor: Path
    kind: str  # "video" | "audio"

    @property
    def parent(self) -> Path:
        """Directory the clip lives in, for roll-grouping purposes.

        For a single file: the file's parent. For an RDC: the RDC's
        parent (typically an RDM directory). The roll-grouping algorithm
        treats RDCs as opaque, so it never descends into them.
        """
        return self.anchor.parent


@dataclass(frozen=True)
class Issue:
    """A discovered file or directory that we won't silently swallow.

    Issues are raised during ``find_clips`` for situations where a file
    is "media-shaped" (a known professional format) but appears in a
    place that breaks our usual one-clip-equals-one-file rule. The
    paradigm case is a stray ``.R3D`` outside any ``.RDC`` directory —
    it's clip data we can't safely attribute to any logical clip, so
    refusing to fabricate numbers is more useful than guessing.

    The roll the issue belongs to is determined by ``anchor``'s position
    in the directory tree, the same way clip-to-roll grouping works.

    Attributes:
        anchor: The offending path. For a stray file, the file itself.
            For an empty ``.RDC`` directory, the directory itself.
        message: Human-readable description of what's wrong, used when
            we print diagnostics to stderr.
    """

    anchor: Path
    message: str

    @property
    def parent(self) -> Path:
        """Directory the issue lives in, for roll-grouping purposes."""
        return self.anchor.parent


# ---------------------------------------------------------------------------
# Clip discovery
# ---------------------------------------------------------------------------


def _is_rdc_dir(p: Path) -> bool:
    """True iff ``p`` is a directory whose name ends in ``.RDC`` (any case)."""
    return p.is_dir() and p.suffix.lower() == RDC_DIR_EXT


def _r3d_files_in_rdc(rdc: Path) -> list[Path]:
    """Return the sequential R3D chunks inside an RDC, sorted by name."""
    try:
        chunks = [f for f in rdc.iterdir() if f.is_file() and f.suffix.lower() == R3D_FILE_EXT]
    except OSError:
        return []
    chunks.sort(key=lambda p: p.name)
    return chunks


def _video_files_in_rdc(rdc: Path) -> list[Path]:
    """Return non-R3D video files inside an RDC, sorted by name.

    RED cameras (Komodo, DSMC3) can record ProRes or H.265 directly
    into the RDC directory structure instead of R3D. In that case the
    RDC contains .mov/.mp4/.mxf files rather than .R3D chunks.
    """
    try:
        files = [f for f in rdc.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
    except OSError:
        return []
    files.sort(key=lambda p: p.name)
    return files


def _rdc_to_clip_or_issue(rdc: Path) -> Clip | Issue:
    """Build a single video Clip from an ``.RDC`` directory, or an Issue if empty.

    Looks for R3D chunks first, then ProRes/H.265 recorded directly into
    the RDC (RED Komodo / DSMC3 non-RAW modes).
    """
    chunks = _r3d_files_in_rdc(rdc) or _video_files_in_rdc(rdc)
    if not chunks:
        return Issue(anchor=rdc, message=f"empty RDC directory (no media files inside): {rdc}")
    # rdc.stem strips the .RDC suffix for the rushes-log clip name.
    return Clip(name=rdc.stem, files=tuple(chunks), anchor=rdc, kind="video")


def _should_skip(name: str) -> bool:
    """macOS litter that pollutes cross-platform card copies."""
    return name == ".DS_Store" or name.startswith("._")


def _scan_dir_files(d: Path, filenames: list[str]) -> tuple[list[Clip], list[Issue]]:
    """Classify the plain files at one directory level into clips and issues.

    Videos are gathered into a per-directory batch first because GoPro
    chaptering can merge multiple files into one Clip (like RDC dirs merge
    R3D chunks); the batch is handed to ``_build_video_clips_in_dir`` which
    knows that rule. Audio files are simpler — one Clip per file. Stray
    .R3D / RED sidecars (RDC dirs were already pruned by the caller) and are
    reported as issues.
    """
    clips: list[Clip] = []
    issues: list[Issue] = []
    dir_videos: list[Path] = []
    for fn in filenames:
        if _should_skip(fn):
            continue
        f = d / fn
        ext = f.suffix.lower()
        if ext == INSV_LRV_EXT:
            # Insta360 camera-generated low-resolution proxy. Silently
            # skip — it is a sidecar to the matching .insv clip and should
            # not be counted as a separate clip or flagged as unexpected.
            continue
        if ext in VIDEO_EXTS:
            dir_videos.append(f)
        elif ext in AUDIO_EXTS:
            clips.append(Clip(name=f.stem, files=(f,), anchor=f, kind="audio"))
        elif ext == R3D_FILE_EXT:
            # RDC dirs were pruned before the file loop, so any .R3D here
            # is in a non-RDC parent — definitively stray.
            issues.append(Issue(anchor=f, message=f"stray .R3D file outside any .RDC directory: {f}"))
        elif ext in RED_SIDECAR_EXTS:
            # Same reasoning — RED color sidecars belong inside an RDC.
            issues.append(Issue(anchor=f, message=f"stray RED sidecar ({ext}) outside any .RDC directory: {f}"))

    if dir_videos:
        clips.extend(_build_video_clips_in_dir(dir_videos))
    return clips, issues


def _parse_gopro_chapter(stem: str) -> tuple[str, str, int, int] | None:
    """Parse a GoPro chaptered video filename into its components.

    The pattern is ``G[H|X|P][zz][xxxx]`` where:
      - second letter is the codec (H = AVC, X = HEVC, P = older)
      - zz is the chapter number (01-99); 01 marks the first chunk of a
        take, 02+ continues the same recording past the FAT32 / 12 GB
        size ceiling
      - xxxx is the take/file number, identical across chunks of one
        recording

    Returns:
        ``(prefix, xxxx, chapter, file_no)`` if the stem matches, where
        ``prefix`` is the two-letter encoding prefix (``GH``/``GX``/``GP``),
        ``xxxx`` is the four-character take string (preserved as-is for
        sort stability), ``chapter`` is the int zz, and ``file_no`` is
        the int xxxx. Returns ``None`` if the stem doesn't match — the
        caller treats it as a regular non-chaptered video file.
    """
    m = _GOPRO_CHAPTER_RE.match(stem)
    if not m:
        return None
    return f"G{m.group(1).upper()}", m.group(3), int(m.group(2)), int(m.group(3))


def _build_video_clips_in_dir(video_files: list[Path]) -> list[Clip]:
    """Turn a directory's worth of video files into Clip objects.

    Most files map 1:1 to a Clip. The exception is GoPro chaptered
    recordings: chunks of the same take (matching ``[GH|GX|GP][zz][xxxx]``
    with shared ``xxxx``) are merged into a single Clip whose ``name``
    is the lowest-chapter chunk's stem and whose ``files`` are all the
    chunks ordered by chapter number.

    Args:
        video_files: list of plain-file video paths in one directory.
            All files must live in the same directory — chaptering is
            only ever a within-directory concern, never cross-directory.

    Returns:
        Clip list, in the order discovered (caller may re-sort).
    """
    # First pass: separate GoPro-chaptered candidates from regular files.
    # We bucket chaptered candidates by ``xxxx`` (the take number).
    gopro_takes: dict[str, list[Path]] = {}
    regular: list[Path] = []
    for f in video_files:
        info = _parse_gopro_chapter(f.stem)
        if info is None:
            regular.append(f)
            continue
        _, xxxx, _chap, _ = info
        gopro_takes.setdefault(xxxx, []).append(f)

    clips: list[Clip] = []

    # Each take of GoPro chaptered video becomes one clip.
    for chunks in gopro_takes.values():
        # Sort chunks by chapter number so the lowest-chapter chunk is
        # first and supplies the canonical clip name. We re-parse here
        # rather than threading the parse through; it's cheap and keeps
        # the bucket-build above readable.
        chunks_sorted = sorted(
            chunks,
            key=lambda p: (_parse_gopro_chapter(p.stem) or ("", "", 0, 0))[2],
        )
        # Anchor on the first chunk so roll-grouping (which keys off
        # parent dir) sees the clip in the same directory as the files.
        anchor = chunks_sorted[0]
        clips.append(
            Clip(
                name=anchor.stem,
                files=tuple(chunks_sorted),
                anchor=anchor,
                kind="video",
            )
        )

    # Regular (non-chaptered) videos: one Clip per file.
    clips.extend(Clip(name=f.stem, files=(f,), anchor=f, kind="video") for f in regular)

    return clips


def find_clips(root: Path) -> tuple[list[Clip], list[Issue]]:
    """Walk ``root`` and return ``(clips, issues)`` discovered under it.

    The walk is RDC-aware: when we encounter a directory whose name ends
    in ``.RDC``, we emit one ``Clip`` for the whole directory and do
    *not* descend into it (so the R3D chunks aren't double-counted as
    individual clips). Everywhere else, we treat each media file as its
    own clip.

    Args:
        root: directory to walk

    Returns:
        ``(clips, issues)`` where clips is sorted by anchor path
        (for stable grouping) and issues are in walk order. Either
        or both may be empty.
    """
    clips: list[Clip] = []
    issues: list[Issue] = []

    # Edge case: user pointed mrl directly at an RDC. Treat the whole
    # thing as a single clip (or raise an issue if it's empty) and stop.
    if _is_rdc_dir(root):
        item = _rdc_to_clip_or_issue(root)
        if isinstance(item, Clip):
            clips.append(item)
        else:
            issues.append(item)
        return clips, issues

    # Sony XDCAM sub-stream directory names that contain proxy/sub-stream
    # MP4s (e.g. XDROOT/Sub).  These are camera-generated delivery
    # proxies, not camera originals, and must never be counted as clips
    # or contribute to session-signature checking.
    _SONY_PROXY_DIRS = {"sub"}

    # Use os.walk so we can prune RDC subtrees: when an RDC is found in
    # `dirs`, we emit it as a Clip and remove it from `dirs` so the
    # walker doesn't descend.
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)

        # Prune Sony proxy/sub-stream directories before touching anything
        # else so neither their files nor their subdirectories are visited.
        dirnames[:] = [n for n in dirnames if n.lower() not in _SONY_PROXY_DIRS]

        # Promote any RDC subdirectory in this level to a Clip and stop
        # the walker from going inside it. Mutate dirnames in place per
        # os.walk's documented contract.
        rdc_names = [n for n in dirnames if n.lower().endswith(RDC_DIR_EXT)]
        for n in rdc_names:
            item = _rdc_to_clip_or_issue(d / n)
            if isinstance(item, Clip):
                clips.append(item)
            else:
                issues.append(item)
        # Prevent os.walk from descending into the RDCs we just consumed.
        dirnames[:] = [n for n in dirnames if not n.lower().endswith(RDC_DIR_EXT)]

        # Now handle plain files at this level.
        dir_clips, dir_issues = _scan_dir_files(d, filenames)
        clips.extend(dir_clips)
        issues.extend(dir_issues)

    # Stable sort by anchor path so caller gets predictable order.
    clips.sort(key=lambda c: str(c.anchor))
    return clips, issues


# ---------------------------------------------------------------------------
# Known-parent directory detection and roll discovery
# ---------------------------------------------------------------------------

# Known-parent and known-sibling patterns are loaded from mrl_presets.toml
# at startup (see _load_roll_detection_config above). The predicates below
# delegate to the compiled values in _RD so the rest of the codebase is
# unchanged.


def _is_known_sibling(name: str) -> bool:
    """Return True if ``name`` should always be excluded from roll discovery.

    Checks against known_siblings_exact (exact, case-insensitive) and
    known_siblings_contains (substring, case-insensitive) from mrl_presets.toml.
    """
    low = name.lower()
    rd = _rd()
    if low in rd["ks_exact"]:
        return True
    return any(word in low for word in rd["ks_contains"])


def _is_known_parent(name: str) -> bool:
    """Return True if ``name`` is a known-parent directory name.

    Checks against known_parents (exact), known_parents_cam_prefix,
    known_parents_cam_suffix, and known_parents_flex (regexes) from
    mrl_presets.toml.
    """
    low = name.lower()
    rd = _rd()
    if low in rd["kp_exact"]:
        return True
    if rd["kp_cam_prefix"].match(low) or rd["kp_cam_suffix"].search(low):
        return True
    return any(p.match(low) for p in rd["kp_flex"])


def _has_media(directory: Path) -> bool:
    """Return True if ``directory`` contains any media file anywhere in its tree."""
    try:
        for _, _, files in os.walk(directory):
            for f in files:
                ext = Path(f).suffix.lower()
                if ext in VIDEO_EXTS or ext in AUDIO_EXTS or ext == R3D_FILE_EXT:
                    return True
    except OSError:
        pass
    return False


def _split_known_parents(d: Path) -> tuple[list[Path], list[Path]] | None:
    """Split ``d``'s immediate subdirs into (known-parents, non-known-parents).

    Subdirs are sorted by name and known siblings are excluded entirely.
    Returns ``None`` if ``d`` can't be read (the caller decides how to
    treat an unreadable directory).
    """
    try:
        subdirs = sorted(
            (s for s in d.iterdir() if s.is_dir() and not _is_known_sibling(s.name)),
            key=lambda s: s.name,
        )
    except OSError:
        return None
    kp = [s for s in subdirs if _is_known_parent(s.name)]
    non_kp = [s for s in subdirs if not _is_known_parent(s.name)]
    return kp, non_kp


def _misplaced_roll_warnings(
    siblings: list[Path],
    kp_name: str,
    parent_name: str,
) -> list[tuple[Path, list[str]]]:
    """Build ``(dir, [warning])`` entries for non-known-parent siblings.

    A non-KP directory sitting alongside a known parent is a likely
    misplaced roll, but only flagged when it actually contains media —
    empty siblings are ignored silently.
    """
    out: list[tuple[Path, list[str]]] = []
    for sib in siblings:
        if not _has_media(sib):
            continue
        warn = (
            f"⚠️  '{sib.name}' found alongside known-parent "
            f"'{kp_name}' under '{parent_name}' — "
            f"possible misplaced roll. Including in output."
        )
        out.append((sib, [warn]))
    return out


def _find_roll_dirs(root: Path) -> list[tuple[Path, list[str]]]:
    """Return ``[(roll_dir, warnings), ...]`` for rolls under ``root``.

    Algorithm:
      1. Inspect ``root``'s immediate subdirectories.
      2. Split into known-parents (KP1) and non-KP siblings at root level.
      3. If no KP1 found → ``root`` itself is the roll.
      4. For each KP1:
         a. Inspect its children, split into KP2 and non-KP2.
         b. If KP2 found → rolls live one level below each KP2 child.
            Non-KP2 siblings of KP2 are included as rolls with a warning.
         c. If no KP2 → non-KP2 children are rolls directly.
      5. Non-KP siblings of KP1 at root level are included as rolls
         with a warning (likely misplaced alongside a KP sibling).
    """
    split = _split_known_parents(root)
    if split is None:
        return [(root, [])]
    kp1, non_kp1 = split

    if not kp1:
        if _is_known_parent(root.name):
            # root itself is a KP (e.g. user runs from AUDIO/ or CAMERA/).
            # Its non-KP children are rolls directly.
            return [(d, []) for d in non_kp1] or [(root, [])]
        # No known parents anywhere → root is the roll itself.
        return [(root, [])]

    results: list[tuple[Path, list[str]]] = []
    results.extend(_misplaced_roll_warnings(non_kp1, kp1[0].name, root.name))

    for parent in kp1:
        child_split = _split_known_parents(parent)
        if child_split is None:
            continue
        kp2, non_kp2 = child_split

        if not kp2:
            # No KP2 → direct non-KP children are rolls.
            results.extend((roll_dir, []) for roll_dir in non_kp2)
            continue

        # Level-2 known parents — rolls live under them.
        for kp in kp2:
            try:
                roll_children = sorted(
                    (d for d in kp.iterdir() if d.is_dir()),
                    key=lambda d: d.name,
                )
            except OSError:
                continue
            results.extend((roll_dir, []) for roll_dir in roll_children)
        results.extend(_misplaced_roll_warnings(non_kp2, kp2[0].name, parent.name))

    return results or [(root, [])]


# ---------------------------------------------------------------------------
# Roll validation (cross-device contamination detection)
# ---------------------------------------------------------------------------


def _is_under_sony_wrapper(p: Path, wrapper: str) -> bool:
    """True iff ``p`` has an ancestor directory named exactly ``wrapper``.

    Used to detect whether a clip lives under ``XDROOT/`` or ``M4ROOT/``.
    Comparison is case-insensitive because the wrappers can be uppercase
    or lowercase depending on the camera firmware.
    """
    target = wrapper.lower()
    return any(part.lower() == target for part in p.parts)


def validate_roll(clips: list[Clip]) -> list[Issue]:
    """Detect cross-device contamination on a single roll.

    A correctly-formatted card from one shoot carries content from
    exactly one device's recording session. Two markers indicate that
    a card was used in two different cameras (or two camera modes, or
    the same camera in two sessions without an intervening format) and
    the clips landed on the same roll directory:

    1. **Sony dual-wrapper populated.** The card has clips under both
       ``XDROOT/`` (XAVC mode) and ``M4ROOT/`` (XAVC-S mode). One
       populated and the other empty is fine — some Sony cameras create
       both directory trees by default. Both populated means the card
       saw two recording modes.

    2. **Mixed session signature.** Each video clip name reduces to a
       session signature (the sequence of letter-runs before the first
       underscore — see ``_session_signature`` for details). All video
       clips on one roll should share the same signature; cameras don't
       change naming schemes mid-session. A mismatch means the card
       carries content from different recording sessions: different
       Sony slot letters (``C001C001`` vs ``A001C001``), different
       GoPro encodings (``GH010001`` vs ``GX010002``), different
       custom-prefix cameras (``ABABABAB001`` vs ``DADADADA002``), or
       even structurally different naming conventions on the same card.

    Audio clips are exempt from the signature check because soundies
    routinely rename WAVs with scene/take labels (``SC_15A_TK1``,
    ``room_tone_restaurant``, etc.).

    Args:
        clips: roll's clip list (post-grouping).

    Returns:
        List of ``Issue``s anchored to representative clip paths.
        Empty if the roll passes all checks. Each issue's ``anchor``
        points at one of the offending files so the user can locate
        the contamination quickly.
    """
    issues: list[Issue] = []
    videos = [c for c in clips if c.kind == "video"]
    if not videos:
        return issues

    # --- Check 1: Sony dual-wrapper populated --------------------------
    in_xdroot = [c for c in videos if _is_under_sony_wrapper(c.anchor, "xdroot")]
    in_m4root = [c for c in videos if _is_under_sony_wrapper(c.anchor, "m4root")]
    if in_xdroot and in_m4root:
        ex_xd = in_xdroot[0].anchor
        ex_m4 = in_m4root[0].anchor
        issues.append(
            Issue(
                anchor=ex_xd,
                message=(
                    f"both XDROOT and M4ROOT populated — card was used in "
                    f"two Sony cameras or two recording modes (XAVC + "
                    f"XAVC-S). Examples: {ex_xd} and {ex_m4}"
                ),
            )
        )

    # --- Check: Mixed session signature across video clips -----------
    # Group video clips by their session signature. If we see more than
    # one signature, the roll mixes content from different sessions.
    by_signature: dict[tuple[str, ...], list[Clip]] = {}
    for c in videos:
        sig = _session_signature(c.name)
        by_signature.setdefault(sig, []).append(c)

    if len(by_signature) > 1:
        # Build a human-readable summary using one example clip name
        # per signature group, plus the count.
        summary_parts = []
        for _sig, group in sorted(
            by_signature.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),  # largest group first
        ):
            example_name = group[0].name
            n = len(group)
            summary_parts.append(f"{example_name!r} ({n} clip{'s' if n != 1 else ''})")
        summary = ", ".join(summary_parts)

        # Anchor on the first clip of the smallest group (likely the
        # contaminating intruder, the one the user most needs to see).
        smallest_sig = min(by_signature, key=lambda s: len(by_signature[s]))
        anchor = by_signature[smallest_sig][0].anchor
        issues.append(
            Issue(
                anchor=anchor,
                message=(
                    f"contains clips from multiple recording sessions — "
                    f"different filename signatures detected: {summary}. "
                    f"Card was likely used in two devices or two sessions "
                    f"without reformatting. First example of the minority "
                    f"group: {anchor}"
                ),
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Audio creation-date sorting (ffprobe)
# ---------------------------------------------------------------------------

#: Concurrent ffprobe subprocesses. Each probe is header-read + exit, so the
#: bottleneck is process startup and volume latency, not CPU — a small pool
#: gives most of the win without hammering slow external drives.
_FFPROBE_WORKERS = 8

# Match a sortable timestamp in any common form recorders write.
_DATE_PREFIX_RE = re.compile(r"^(\d{4})[-:](\d{2})[-:](\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?")


def _normalize_timestamp(s: str) -> str:
    """Normalise a recorder timestamp to a sortable ISO-ish string.

    Returns ``""`` if the input doesn't look like a date or carries the
    all-zeros placeholder some recorders write when their clock is unset.
    """
    if not s:
        return ""
    m = _DATE_PREFIX_RE.match(s.strip())
    if not m:
        return ""
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if int(y) == 0 and int(mo) == 0 and int(d) == 0:
        return ""
    h = m.group(4) or "00"
    mi = m.group(5) or "00"
    sec = m.group(6) or "00"
    return f"{y}-{mo}-{d}T{h}:{mi}:{sec}"


def audio_creation_times(wavs: list[Path]) -> dict[Path, str]:
    """Return ``{path: sortable_timestamp}`` for each WAV, via ffprobe.

    ffprobe cannot batch multiple files into one JSON document, so one
    subprocess is spawned per file — in parallel (see ``_FFPROBE_WORKERS``),
    since the work is all subprocess wait time. Probes BWF/iXML tags stored
    in the WAV format container: ``creation_time``, ``origination_date``
    + ``origination_time``, ``date``. The first usable value wins. Missing or
    unparsable values map to empty strings; the caller falls back to
    ``st_mtime``.
    """
    if not wavs:
        return {}

    args = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_entries",
        "format_tags=creation_time,origination_date,origination_time,date",
    ]

    def _probe(p: Path) -> str:
        raw = run_capture([*args, str(p)])
        if not raw.strip():
            return ""
        try:
            tags = json.loads(raw).get("format", {}).get("tags", {})
            # Normalise key case — ffprobe lowercases tag names.
            tags = {k.lower(): v for k, v in tags.items()}
        except (json.JSONDecodeError, AttributeError):
            return ""
        # Priority order: combine origination_date + origination_time
        # (BWF spec fields) first, then creation_time (RF64/iXML).
        orig_date = tags.get("origination_date", "")
        orig_time = tags.get("origination_time", "")
        ts = ""
        if orig_date:
            ts = _normalize_timestamp(f"{orig_date} {orig_time}".strip())
        if not ts:
            ts = _normalize_timestamp(tags.get("creation_time", ""))
        if not ts:
            ts = _normalize_timestamp(tags.get("date", ""))
        return ts

    with ThreadPoolExecutor(max_workers=min(_FFPROBE_WORKERS, len(wavs))) as pool:
        return dict(zip(wavs, pool.map(_probe, wavs), strict=True))


def _sort_key_for_audio(path: Path, embedded_ts: str) -> str:
    """Return a sortable key for a WAV: embedded timestamp, else mtime fallback.

    Embedded timestamps are recorder-written and survive copies between
    drives. We prefer them. The mtime fallback uses microsecond precision
    so files written within the same second still sort by recording
    order; embedded BWF timestamps are second-precision and unlikely to
    collide on a real recorder.
    """
    if embedded_ts:
        return embedded_ts
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# First / Last clips
# ---------------------------------------------------------------------------


def first_last_clips(clips: list[Clip]) -> tuple[str, str]:
    """Return ``(first_clip, last_clip)`` for one roll.

    Rules:
      - If any video clips are present, only video clips are considered
        for endpoints — sorted by filesystem creation time (st_birthtime
        on macOS, mtime fallback; see ``_video_ctime``). This avoids a
        sidecar WAV becoming a misleading "last clip". Caveat: creation
        times only reflect recording order if the copy tool preserved
        them — clip names, being sequential, may disagree after a lossy
        offload.
      - For audio-only rolls of WAV files, sort by creation date because
        alphabetical order can lie.
      - For audio-only rolls containing **any ZAX files** (Zaxcom MARF
        format) or **MIC files** (Sound Devices proprietary format), sort
        everything alphabetically. These formats are unreadable by ffprobe,
        but both recorders auto-name them sequentially so alphabetical order
        matches recording order. When a roll mixes WAV with ZAX or MIC
        (rare), we also fall back to alphabetical to keep the ordering rule
        consistent within the roll.

    Returns:
        ``(first, last)`` as bare names without extension. Both equal if
        only one clip exists. Empty strings if the roll is empty.
    """
    if not clips:
        return "", ""

    videos = [c for c in clips if c.kind == "video"]
    if videos:

        def _video_ctime(c: Clip) -> float:
            """Filesystem creation date for sorting video clips.

            Uses st_birthtime (macOS), st_ctime (Windows), or
            st_mtime (Linux/other) — whichever is available. Falls
            back to 0.0 so unreadable files sort to the front rather
            than crashing. For RDC clips, uses the first R3D chunk.
            """
            try:
                st = c.files[0].stat()
                return getattr(st, "st_birthtime", None) or st.st_mtime
            except OSError:
                return 0.0

        ordered = sorted(videos, key=_video_ctime)
        return ordered[0].name, ordered[-1].name

    # Audio-only roll. Decide which sort strategy applies based on the
    # mix of audio formats present.
    audios = [c for c in clips if c.kind == "audio"]
    has_zax = any(c.files[0].suffix.lower() in {ZAX_EXT, MIC_EXT} for c in audios)

    if has_zax:
        # Alphabetical works for ZAX (sequential recorder naming) and MIC
        # (Sound Devices proprietary, also unreadable by ffprobe) and
        # is a safe fallback for any mixed-format audio roll.
        ordered_audio = sorted(audios, key=lambda c: c.name)
        return ordered_audio[0].name, ordered_audio[-1].name

    # All-WAV audio roll: probe BWF dates and sort by recording timestamp.
    wav_paths = [c.files[0] for c in audios]
    ts_map = audio_creation_times(wav_paths)
    ordered_audio = sorted(
        audios,
        key=lambda c: _sort_key_for_audio(c.files[0], ts_map.get(c.files[0], "")),
    )
    return ordered_audio[0].name, ordered_audio[-1].name


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------


def total_size_gb(clips: list[Clip], roll_root: Path | None = None) -> float:
    """Return the roll size in GB (macOS/Linux) or GiB (Windows).

    When ``roll_root`` is provided, walks the entire directory tree so
    the result matches what Finder/Explorer reports — sidecar files
    (.XML, .BIM, .SMI, .cube, .mhl, etc.) are included. Falls back to
    summing only the media files in ``clips`` when no root is given.
    """
    total_bytes = 0
    if roll_root is not None:
        try:
            for dirpath, _, filenames in os.walk(roll_root):
                for fname in filenames:
                    if _should_skip(fname):
                        continue
                    try:
                        total_bytes += (Path(dirpath) / fname).stat().st_size
                    except OSError:
                        continue
        except OSError:
            pass
    else:
        for c in clips:
            for f in c.files:
                try:
                    total_bytes += f.stat().st_size
                except OSError:
                    continue
    return total_bytes / _SIZE_DIVISOR


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


def total_duration_seconds(clips: list[Clip]) -> float:
    """Aggregate video duration across all clips via ffprobe.

    Audio clips contribute zero (the rushes log doesn't track duration
    for sound rolls). Video file paths are flattened across all clips —
    including every R3D chunk inside RDCs.

    One ffprobe subprocess is spawned per file using ``-show_entries
    format=duration``, which reads only the container header and exits
    immediately — fast even on large files over slow external volumes.
    Files are probed in parallel (see ``_FFPROBE_WORKERS``); the work is
    all subprocess wait time. The concat-demuxer batching approach was
    tried previously but proved unreliable: ffprobe treats the manifest
    as one virtual input so per-file durations are not individually
    accessible.

    Returns 0.0 if ffprobe is not installed, no video clips are present,
    or all files failed to parse.
    """
    video_files: list[Path] = []
    for c in clips:
        if c.kind != "video":
            continue
        video_files.extend(c.files)

    if not video_files:
        return 0.0

    def _probe_duration(f: Path) -> float:
        raw = run_capture(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration",
                str(f),
            ]
        )
        if not raw.strip():
            return 0.0
        try:
            dur = json.loads(raw).get("format", {}).get("duration", "")
            return float(dur) if dur else 0.0
        except (json.JSONDecodeError, ValueError, AttributeError):
            return 0.0

    with ThreadPoolExecutor(max_workers=min(_FFPROBE_WORKERS, len(video_files))) as pool:
        return sum(pool.map(_probe_duration, video_files))


def format_hms(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS``, rounded to whole seconds."""
    total = round(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Per-roll line assembly
# ---------------------------------------------------------------------------


def render_roll(clips: list[Clip], fields: list[str], roll_root: Path | None = None) -> list[str]:
    """Build the ordered list of cell values for one roll (no roll label).

    ``fields`` is an ordered list of field keys to emit, derived from the
    order flags were given on the command line. Valid keys:
    ``"first"``, ``"last"``, ``"n"``, ``"duration"``, ``"size"``.
    ``"roll"`` is handled by the caller since it comes from the roll name.
    ``roll_root`` is the directory to walk for size (matches Finder/Explorer).
    """
    # Compute lazily — only what's needed.
    _first_last: tuple[str, str] | None = None

    def get_first_last() -> tuple[str, str]:
        nonlocal _first_last
        if _first_last is None:
            _first_last = first_last_clips(clips)
        return _first_last

    _duration: float | None = None
    _any_video: bool | None = None

    def get_duration() -> str:
        nonlocal _duration, _any_video
        if _duration is None:
            _duration = total_duration_seconds(clips)
            _any_video = any(c.kind == "video" for c in clips)
        return format_hms(_duration) if _any_video else ""

    # Thunk dispatch: each field maps to a zero-arg producer so only the
    # requested columns are computed (first/last share get_first_last).
    emitters = {
        "first": lambda: get_first_last()[0],
        "last": lambda: get_first_last()[1],
        "n": lambda: str(len(clips)),
        "duration": get_duration,
        "size": lambda: f"{total_size_gb(clips, roll_root):.2f}",
    }
    return [emitters[f]() for f in fields if f in emitters]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_FIELD_TO_HEADER = {
    "first": "FIRST CLIP",
    "last": "LAST CLIP",
    "n": "CLIP COUNT",
    "duration": "DURATION",
    "size": f"SIZE ({_SIZE_UNIT})",
    "roll": "ROLL",
}
# Default field order when no field flags are given.
_DEFAULT_FIELDS = ["roll", "first", "last", "n", "duration", "size"]


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser for the mrl CLI."""
    parser = argparse.ArgumentParser(
        prog="mrl",
        description=(
            "Master Rushes Log helper: scan a camera roll directory and "
            "emit values for the log (first clip, last clip, size, "
            "duration, clip count, roll name). With no flags, all fields "
            "are emitted in default order. Multiple rolls under the input "
            "path (or multiple input paths) produce one line per roll. "
            "Short flags can be combined POSIX-style: -flsdc is the same "
            "as -f -l -s -d -c. Column order follows the order flags are given."
        ),
    )
    parser.set_defaults(field_order=[])
    parser.add_argument(
        "-f", "-F", "--first-clip", action=FieldOrderAction, field="first", help="print first clip name"
    )
    parser.add_argument("-l", "-L", "--last-clip", action=FieldOrderAction, field="last", help="print last clip name.")
    parser.add_argument(
        "-s", "-S", "--size", action=FieldOrderAction, field="size", help=f"print total size in {_SIZE_UNIT}"
    )
    parser.add_argument(
        "-d",
        "-D",
        "--duration",
        action=FieldOrderAction,
        field="duration",
        help="print aggregated duration of video clips (HH:MM:SS)",
    )
    parser.add_argument("-c", "-C", "--clip-count", action=FieldOrderAction, field="n", help="print the clip count")
    parser.add_argument("-r", "-R", "--roll", action=FieldOrderAction, field="roll", help="print the roll name")
    parser.add_argument(
        "-E",
        "--edit-presets",
        action="store_true",
        dest="edit_presets",
        help="edit mrl_presets.toml in your default terminal editor ($EDITOR)",
    )
    parser.add_argument(
        "-O",
        "--open-presets",
        action="store_true",
        dest="open_presets",
        help="open mrl_presets.toml in the system default app for text files",
    )
    parser.add_argument("--version", action="version", version=f"{__version__}")
    parser.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="camera roll directories (default: current directory)",
    )
    return parser


def _collect_rolls(args: argparse.Namespace) -> list[tuple[str, Path, list[Clip], list[Issue]]] | None:
    """Discover rolls for the input path(s), validating each roll's contents.

    Multi-path mode treats each argument as a named roll directly (trusting
    the user's grouping). Single-path mode uses ``_find_roll_dirs`` to locate
    rolls via known-parent detection. Returns the collected rolls, or ``None``
    if an input path is not a directory (the error is already printed).
    """
    rolls: list[tuple[str, Path, list[Clip], list[Issue]]] = []
    if len(args.paths) > 1:
        # Multi-path mode: each argument is a named roll directly — no
        # grouping algorithm, insertion order preserved. We trust the names
        # but still validate contents for mixed-session or other issues.
        for raw_path in args.paths:
            target = Path(raw_path).resolve()
            if not target.is_dir():
                print(f"Error: '{target}' is not a directory.", file=sys.stderr)
                return None
            clips, issues = find_clips(target)
            roll_issues = list(issues) + validate_roll(clips)
            rolls.append((target.name, target, clips, roll_issues))
        return rolls

    # Single-path mode (or bare invocation defaulting to "."):
    # use _find_roll_dirs to locate rolls via known-parent detection.
    target = Path(args.paths[0]).resolve()
    if not target.is_dir():
        print(f"Error: '{target}' is not a directory.", file=sys.stderr)
        return None
    for roll_dir, dir_warnings in _find_roll_dirs(target):
        for w in dir_warnings:
            print(w, file=sys.stderr)
        clips, issues = find_clips(roll_dir)
        if not clips and not issues:
            continue
        roll_issues = list(issues) + validate_roll(clips)
        rolls.append((roll_dir.name, roll_dir, clips, roll_issues))
    return rolls


def _report_broken_rolls(rolls: list[tuple[str, Path, list[Clip], list[Issue]]]) -> bool:
    """Print stderr diagnostics for every roll with issues.

    Done before stdout so the user sees warnings first when stderr and
    stdout share a terminal. Returns True if any roll had issues.
    """
    had_issues = False
    for name, _root, _clips, issues in rolls:
        if not issues:
            continue
        had_issues = True
        for issue in issues:
            print(f"Warning: roll '{name}' — {issue.message}", file=sys.stderr)
        print(
            f"Warning: roll '{name}' has issues that need investigation; "
            f"skipping output for this roll. Please check it manually.",
            file=sys.stderr,
        )
    return had_issues


def _fmt_header(label: str) -> str:
    """Dim the label with its first character underlined as a flag hint."""
    if not label:
        return ""
    return f"{DIM_ERR}{UNDERLINE_ERR}{label[0]}{RESET_ERR}{DIM_ERR}{label[1:]}{RESET_ERR}"


def _format_header_line(header_labels: list[str], col_widths: list[int]) -> str:
    """Build the dimmed, column-aligned header row (printed to stderr)."""
    padded = [h.ljust(col_widths[i]) if i < len(col_widths) - 1 else h for i, h in enumerate(header_labels)]
    return "\t".join(_fmt_header(h) for h in padded)


def _raw_row(
    name: str,
    roll_root: Path,
    clips: list[Clip],
    fields: list[str],
    data_fields: list[str],
) -> list[str]:
    """Return ordered cell values (strings) for one roll."""
    data_vals = render_roll(clips, data_fields, roll_root)
    di = 0
    cells: list[str] = []
    for f in fields:
        if f == "roll":
            cells.append(name)
        else:
            cells.append(data_vals[di] if di < len(data_vals) else "")
            di += 1
    return cells


def _pad_row(cells: list[str], widths: list[int], *, pad_last: bool = False) -> str:
    """Space-pad each cell to its column width, join with a single tab.

    The last cell is only padded when ``pad_last=True`` — i.e. when a
    courtesy roll label follows and needs to land in an aligned column.
    ``cells`` and ``widths`` must be the same length; a mismatch is a bug
    but we handle it gracefully rather than crashing.
    """
    n = len(widths)
    padded = []
    for i, cell in enumerate(cells):
        if i < n - 1 or pad_last:
            padded.append(cell.ljust(widths[i] if i < n else 0))
        else:
            padded.append(cell)
    return "\t".join(padded)


def _clipboard_tsv(rows: list[tuple[str, list[str]]]) -> str:
    """Build strict TSV for clipboard: no padding, no header.

    Cells come from ``_raw_row`` which only includes requested fields —
    roll is present iff ``-r`` was given. No courtesy labels, no trailing
    spaces, one row per line.
    """
    return "\n".join("\t".join(cells) for _name, cells in rows)


def _copy_to_clipboard(text: str) -> None:
    """Copy ``text`` to the system clipboard. Silent no-op on failure.

    Tries pyperclip first (handles macOS, Windows, Linux X11/Wayland,
    WSL). Falls back to direct subprocess calls (pbcopy / clip / xclip)
    if pyperclip raises — e.g. no clipboard mechanism available on a
    headless Linux box.
    """
    try:
        pyperclip.copy(text)
        return
    except pyperclip.PyperclipException:
        pass
    # Stdlib fallback.
    try:
        if sys.platform == "darwin":
            invoke(["pbcopy"], stdin_text=text, text=True)
        elif sys.platform == "win32":
            invoke(["clip"], stdin_text=text, text=True)
        else:
            for cmd in (
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
            ):
                if invoke(cmd, stdin_text=text, text=True).returncode == 0:
                    break
    except OSError:
        pass  # Clipboard is best-effort; never crash the script.


def _render_rolls(
    valid_rolls: list[tuple[str, Path, list[Clip]]],
    fields: list[str],
    *,
    multi: bool,
    had_issues: bool,
) -> int:
    """Render surviving rolls: values to stdout, aligned header to stderr.

    Copies the strict (unpadded) TSV to the clipboard. The table layout
    (header + one row per roll) is used whenever more than one roll was
    found; a lone roll gets the compact single-roll layout. Returns the
    process exit code: 1 if any roll had issues, else 0.
    """
    want_roll = "roll" in fields
    data_fields = [f for f in fields if f != "roll"]
    header_labels = [_FIELD_TO_HEADER[f] for f in fields]

    all_rows: list[tuple[str, list[str]]] = [
        (name, _raw_row(name, root, clips, fields, data_fields)) for name, root, clips in valid_rolls
    ]

    # Column widths: max of header label length and any data cell in that column.
    col_widths = [len(h) for h in header_labels]
    for _name, cells in all_rows:
        for i, cell in enumerate(cells):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))

    if multi:
        # Header to stderr — purely visual, never captured by pipes.
        print(_format_header_line(header_labels, col_widths), file=sys.stderr)
        for name, cells in all_rows:
            if want_roll:
                print(_pad_row(cells, col_widths))
            else:
                # Courtesy roll label as a trailing dimmed column, excluded
                # from clipboard TSV since it's not in cells.
                row = _pad_row(cells, col_widths, pad_last=True)
                print(f"{row}\t{DIM}({name}){RESET}")
        _copy_to_clipboard(_clipboard_tsv(all_rows))
        return 1 if had_issues else 0

    # Single valid roll.
    _name, cells = all_rows[0]
    if len(fields) > 1:
        print(_format_header_line(header_labels, col_widths), file=sys.stderr)
    else:
        print(_fmt_header(header_labels[0]), file=sys.stderr)
    print(_pad_row(cells, col_widths))
    _copy_to_clipboard(_clipboard_tsv(all_rows))
    return 1 if had_issues else 0


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and emit one rushes-log line per roll.

    The flow is:

      1. Parse flags. Combined POSIX-style flags work (``-csdn``).
         If no field flags are passed, all of them are emitted.
      2. For each input path, run ``find_clips`` to discover clips
         and issues, then identifies rolls via known-parent detection.
      3. For each resulting roll, run ``validate_roll`` to detect
         cross-device contamination (mixed slot prefixes, GoPro
         encodings, Sony XDROOT+M4ROOT both populated). Merge any
         resulting issues with discovery-time issues.
      4. Print stderr diagnostics for every broken roll, then drop
         broken rolls from stdout output entirely (so partial values
         like size or count don't end up pasted into the log).
      5. Render the surviving rolls. Always tab-separated values,
         space-padded so columns align visually in the terminal.
         Also copies strict TSV (no padding) to the system clipboard.

    Returns:
        Exit code: ``0`` on full success, ``1`` if any roll had issues
        (whether or not other rolls produced output) or if no media
        was found at all.
    """
    args = _build_parser().parse_args(argv)

    # -O/-E: open presets in the default app or $EDITOR, then exit —
    # these flags short-circuit everything else.
    if maybe_open_config(PRESETS_PATH, open_app=args.open_presets, edit=args.edit_presets, label="presets config file"):
        return 0

    fields = args.field_order or _DEFAULT_FIELDS[:]

    rolls = _collect_rolls(args)
    if rolls is None:
        return 1  # invalid input path; error already reported
    if not rolls:
        print("Error: no media files found under any input path.", file=sys.stderr)
        return 1

    had_issues = _report_broken_rolls(rolls)

    # Drop broken rolls from stdout. Even if -s and -n would technically
    # be unaffected by the issue, emitting any partial value risks the
    # DIT pasting it into the log without realizing the roll is suspect.
    valid_rolls = [(n, root, c) for (n, root, c, i) in rolls if not i]
    if not valid_rolls:
        # Every roll was broken. Stderr already explained why; just
        # exit non-zero so calling shells/scripts notice.
        return 1

    return _render_rolls(valid_rolls, fields, multi=len(rolls) > 1, had_issues=had_issues)


if __name__ == "__main__":
    sys.exit(main())
