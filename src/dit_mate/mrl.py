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
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_config_dir

# ---------------------------------------------------------------------------
# Roll-detection config  (loaded from mrl_presets.toml in user config dir)
# ---------------------------------------------------------------------------

# Version is imported from dit-mate
__version__ = importlib.metadata.version("dit-mate")

PRESETS_FILENAME   = "mrl_presets.toml"
DEFAULT_PRESETS    = "mrl_default_presets.toml"
CONFIG_DIR         = Path(user_config_dir("dit-mate"))
PRESETS_PATH       = CONFIG_DIR / PRESETS_FILENAME
BUNDLED_PRESETS    = Path(__file__).parent / "data" / DEFAULT_PRESETS

_REQUIRED_KEYS = {
    "known_parents",
    "known_parents_flex",
    "known_parents_cam_prefix",
    "known_parents_cam_suffix",
    "known_siblings_exact",
    "known_siblings_contains",
}


def _load_roll_detection_config() -> dict:
    """Load and validate the [roll_detection] table from mrl_presets.toml.

    On first run the file does not exist; it is seeded from the bundled
    mrl_default_presets.toml that ships with the package, then read back.
    On every subsequent run the user's live copy is used instead, so
    customisations (extra camera names, site-specific sibling patterns)
    survive package updates.
    """
    if not PRESETS_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not BUNDLED_PRESETS.exists():
            sys.exit(
                f"❌  Bundled presets not found: {BUNDLED_PRESETS}\n"
                f"    Re-installing dit-mate should fix this."
            )
        shutil.copy(BUNDLED_PRESETS, PRESETS_PATH)

    try:
        with open(PRESETS_PATH, "rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(
            f"❌  Could not parse presets config file: {PRESETS_PATH}\n"
            f"    {exc}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )

    if "roll_detection" not in data:
        sys.exit(
            f"❌  Missing [roll_detection] table in {PRESETS_PATH}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )

    cfg = data["roll_detection"]
    missing = _REQUIRED_KEYS - set(cfg.keys())
    if missing:
        sys.exit(
            f"❌  [roll_detection] in {PRESETS_PATH} is missing required keys:\n"
            f"    {', '.join(sorted(missing))}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )

    # Type-check list fields
    for list_key in (
        "known_parents",
        "known_parents_flex",
        "known_siblings_exact",
        "known_siblings_contains",
    ):
        if not isinstance(cfg[list_key], list):
            sys.exit(
                f"❌  [roll_detection].{list_key} must be a list of quoted strings.\n"
                f"    Run 'mrl -E' to edit the file and fix the error."
            )

    # Type-check string fields
    for str_key in ("known_parents_cam_prefix", "known_parents_cam_suffix"):
        if not isinstance(cfg[str_key], str):
            sys.exit(
                f"❌  [roll_detection].{str_key} must be a quoted string (regex pattern).\n"
                f"    Run 'mrl -E' to edit the file and fix the error."
            )

    # Compile regex fields — catch bad patterns immediately so the error
    # message points to the config file rather than crashing inside the
    # roll-detection logic later on.
    try:
        compiled_flex = [
            re.compile(pat, re.IGNORECASE) for pat in cfg["known_parents_flex"]
        ]
    except re.error as exc:
        sys.exit(
            f"❌  Bad regex in [roll_detection].known_parents_flex: {exc}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )
    try:
        compiled_cam_prefix = re.compile(cfg["known_parents_cam_prefix"], re.IGNORECASE)
        compiled_cam_suffix = re.compile(cfg["known_parents_cam_suffix"], re.IGNORECASE)
    except re.error as exc:
        sys.exit(
            f"❌  Bad regex in [roll_detection] cam prefix/suffix: {exc}\n"
            f"    Run 'mrl -E' to edit the file and fix the error."
        )

    return {
        "kp_exact":      set(s.lower() for s in cfg["known_parents"]),
        "kp_flex":       compiled_flex,
        "kp_cam_prefix": compiled_cam_prefix,
        "kp_cam_suffix": compiled_cam_suffix,
        "ks_exact":      set(s.lower() for s in cfg["known_siblings_exact"]),
        "ks_contains":   set(s.lower() for s in cfg["known_siblings_contains"]),
    }


_RD = _load_roll_detection_config()


# ---------------------------------------------------------------------------
# Preset editor helpers  (shared style with mkday)
# ---------------------------------------------------------------------------

def _open_presets_with_default_app() -> None:
    """Open mrl_presets.toml in the OS default app for .toml files.

    Uses:
      macOS   → open
      Windows → os.startfile
      Linux   → xdg-open
    """
    if not PRESETS_PATH.exists():
        sys.exit(f"❌  Presets config file not found: {PRESETS_PATH}")

    print(f"📋  Opening presets config file: {PRESETS_PATH}")
    try:
        if sys.platform == "darwin":
            os.execvp("open", ["open", str(PRESETS_PATH)])
        elif sys.platform == "win32":
            os.startfile(str(PRESETS_PATH))  # type: ignore[attr-defined]
        else:
            os.execvp("xdg-open", ["xdg-open", str(PRESETS_PATH)])
    except FileNotFoundError as exc:
        sys.exit(f"❌  Could not open presets file: {exc}")


def _open_presets_in_editor() -> None:
    """Open mrl_presets.toml in the user's preferred terminal editor.

    Editor resolution order:
      1. $EDITOR environment variable
      2. $VISUAL environment variable
      3. nano  (macOS / Linux fallback)
      4. notepad  (Windows fallback)
    """
    if not PRESETS_PATH.exists():
        sys.exit(f"❌  Presets config file not found: {PRESETS_PATH}")

    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or ("notepad" if sys.platform == "win32" else "nano")
    )

    print(f"📝  Opening {PRESETS_PATH} with '{editor}'…")
    try:
        os.execvp(editor, [editor, str(PRESETS_PATH)])
    except FileNotFoundError:
        sys.exit(
            f"❌  Editor not found: '{editor}'\n"
            f"    Set the $EDITOR environment variable to your preferred editor,\n"
            f"    or use -O to open mrl_presets.toml with the system default app."
        )


# ---------------------------------------------------------------------------
# Professional acquisition formats.
# .R3D is intentionally absent here — R3D files are inside RDC directories
# and we surface RDC-as-clip via the discovery layer instead of treating
# raw R3D files as clips.
VIDEO_EXTS = {".mxf", ".mov", ".mp4"}

# RED-specific: directory extension that wraps the R3D chunks of one clip.
RDC_DIR_EXT = ".rdc"
R3D_FILE_EXT = ".r3d"

# RED sidecars that live *inside* an RDC alongside the R3D chunks. We don't
# count them, but if any of these turn up *outside* an RDC it means a copy
# or move went wrong — flag for manual review.
RED_SIDECAR_EXTS = {".rmd", ".rlx", ".rsx"}

# GoPro chaptered recordings. When a take exceeds the FAT32 4 GB limit
# (or the 12 GB ceiling on newer cards), the camera splits it across
# multiple files following the pattern ``[GH|GX|GP][zz][xxxx].MP4``:
#   - GH = AVC encoding, GX = HEVC, GP = older format
#   - zz (chapter, 01–99) increments per chunk of the same recording
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
AUDIO_EXTS = {".wav", ".zax"}
ZAX_EXT = ".zax"

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

def _enable_ansi_on_windows() -> bool:
    """Enable ANSI escape processing on Windows 10+ consoles. No-op on Unix.

    Returns:
        ``True`` if ANSI is usable (Unix always, Windows when the
        SetConsoleMode call succeeds). The caller pairs this with
        ``sys.stdout.isatty()`` to decide whether to emit color codes.
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32.SetConsoleMode(h, mode.value | 0x0004)
        return True
    except Exception:
        return False


_ANSI_OK = sys.stdout.isatty() and _enable_ansi_on_windows()
_ANSI_ERR = sys.stderr.isatty() and _enable_ansi_on_windows()
DIM = "\033[38;5;246m" if _ANSI_OK else ""
RESET = "\033[0m" if _ANSI_OK else ""
# Header is printed to stderr, so it needs its own ANSI guards.
DIM_ERR   = "\033[38;5;246m" if _ANSI_ERR else ""
UNDERLINE_ERR = "\033[4m"   if _ANSI_ERR else ""
RESET_ERR = "\033[0m"        if _ANSI_ERR else ""

# Windows Explorer reports sizes in GiB (1 GiB = 2^30 bytes) while
# macOS Finder and Linux tools report in decimal GB (1 GB = 10^9 bytes).
# We match the convention of the host OS so the numbers agree with what
# the user sees in their file manager.
_SIZE_IS_GIB = os.name == "nt"
_SIZE_DIVISOR = 1_073_741_824 if _SIZE_IS_GIB else 1_000_000_000
_SIZE_UNIT = "GiB" if _SIZE_IS_GIB else "GB"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

# On Windows, suppress child console windows for media tools.
_POPEN_KW: dict = {}
if os.name == "nt":
    _POPEN_KW["creationflags"] = 0x08000000  # CREATE_NO_WINDOW


def _run(cmd: list[str]) -> str:
    """Run a command and return stdout. Empty string on any failure.

    Used to invoke ``ffprobe`` from a single batched
    call per roll. We intentionally swallow all errors (missing binary,
    non-zero exit, OS error) and return an empty string so callers can
    treat "no data" and "tool not installed" identically — both result
    in the corresponding field being skipped or set to a fallback value
    rather than the whole script crashing.

    The Windows ``CREATE_NO_WINDOW`` flag in ``_POPEN_KW`` keeps a
    background console window from briefly flashing when running mrl
    from a non-console environment (e.g. a launcher).
    """
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=False, **_POPEN_KW,
        )
        return r.stdout
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return ""


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
    chunks: list[Path] = []
    try:
        for f in rdc.iterdir():
            if f.is_file() and f.suffix.lower() == R3D_FILE_EXT:
                chunks.append(f)
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
    files: list[Path] = []
    try:
        for f in rdc.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                files.append(f)
    except OSError:
        return []
    files.sort(key=lambda p: p.name)
    return files


def _should_skip(name: str) -> bool:
    """macOS litter that pollutes cross-platform card copies."""
    return name == ".DS_Store" or name.startswith("._")


def _parse_gopro_chapter(stem: str) -> tuple[str, str, int, int] | None:
    """Parse a GoPro chaptered video filename into its components.

    The pattern is ``G[H|X|P][zz][xxxx]`` where:
      - second letter is the codec (H = AVC, X = HEVC, P = older)
      - zz is the chapter number (01–99); 01 marks the first chunk of a
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
    for xxxx, chunks in gopro_takes.items():
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
        clips.append(Clip(
            name=anchor.stem,
            files=tuple(chunks_sorted),
            anchor=anchor,
            kind="video",
        ))

    # Regular (non-chaptered) videos: one Clip per file.
    for f in regular:
        clips.append(Clip(
            name=f.stem, files=(f,), anchor=f, kind="video",
        ))

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
        chunks = _r3d_files_in_rdc(root)
        if not chunks:
            # No R3D — check for ProRes/H.265 recorded directly into the RDC.
            chunks = _video_files_in_rdc(root)
        if chunks:
            clips.append(Clip(
                name=root.stem,  # strips .RDC
                files=tuple(chunks),
                anchor=root,
                kind="video",
            ))
        else:
            issues.append(Issue(
                anchor=root,
                message=f"empty RDC directory (no media files inside): {root}",
            ))
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
            rdc = d / n
            chunks = _r3d_files_in_rdc(rdc)
            if not chunks:
                # No R3D — check for ProRes/H.265 recorded directly into the RDC
                # (RED Komodo, DSMC3 in non-RAW modes).
                chunks = _video_files_in_rdc(rdc)
            if not chunks:
                # Genuinely empty RDC — clip data missing.
                issues.append(Issue(
                    anchor=rdc,
                    message=f"empty RDC directory (no media files inside): {rdc}",
                ))
                continue
            # Strip the .RDC suffix for the rushes-log clip name.
            name_no_ext = n[:-len(RDC_DIR_EXT)] if len(n) > len(RDC_DIR_EXT) else n
            clips.append(Clip(
                name=name_no_ext,
                files=tuple(chunks),
                anchor=rdc,
                kind="video",
            ))
        # Prevent os.walk from descending into the RDCs we just consumed.
        dirnames[:] = [n for n in dirnames if not n.lower().endswith(RDC_DIR_EXT)]

        # Now handle plain files at this level. Videos are gathered
        # into a per-directory list first because GoPro chaptering can
        # merge multiple files into one Clip (similar to how RDC
        # directories merge multiple R3D files); we hand the batch to
        # _build_video_clips_in_dir which knows that rule. Audio files
        # are simpler — one Clip per file.
        dir_videos: list[Path] = []
        for fn in filenames:
            if _should_skip(fn):
                continue
            f = d / fn
            ext = f.suffix.lower()
            if ext in VIDEO_EXTS:
                dir_videos.append(f)
            elif ext in AUDIO_EXTS:
                clips.append(Clip(
                    name=f.stem, files=(f,), anchor=f, kind="audio",
                ))
            elif ext == R3D_FILE_EXT:
                # We pruned RDC dirs from `dirnames` before reaching the
                # file loop, so any .R3D we see here is in a non-RDC
                # parent — definitively stray.
                issues.append(Issue(
                    anchor=f,
                    message=f"stray .R3D file outside any .RDC directory: {f}",
                ))
            elif ext in RED_SIDECAR_EXTS:
                # Same reasoning — RED color sidecars belong inside an RDC.
                issues.append(Issue(
                    anchor=f,
                    message=f"stray RED sidecar ({ext}) outside any .RDC directory: {f}",
                ))

        if dir_videos:
            clips.extend(_build_video_clips_in_dir(dir_videos))

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
    if low in _RD["ks_exact"]:
        return True
    return any(word in low for word in _RD["ks_contains"])


def _is_known_parent(name: str) -> bool:
    """Return True if ``name`` is a known-parent directory name.

    Checks against known_parents (exact), known_parents_cam_prefix,
    known_parents_cam_suffix, and known_parents_flex (regexes) from
    mrl_presets.toml.
    """
    low = name.lower()
    if low in _RD["kp_exact"]:
        return True
    if _RD["kp_cam_prefix"].match(low) or _RD["kp_cam_suffix"].search(low):
        return True
    return any(p.match(low) for p in _RD["kp_flex"])


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
    try:
        subdirs = sorted(
            [d for d in root.iterdir()
             if d.is_dir() and not _is_known_sibling(d.name)],
            key=lambda d: d.name,
        )
    except OSError:
        return [(root, [])]

    kp1     = [d for d in subdirs if _is_known_parent(d.name)]
    non_kp1 = [d for d in subdirs if not _is_known_parent(d.name)]

    if not kp1:
        if _is_known_parent(root.name):
            # root itself is a KP (e.g. user runs from AUDIO/ or CAMERA/).
            # Its non-KP children are rolls directly.
            return [(d, []) for d in non_kp1] or [(root, [])]
        # No known parents anywhere → root is the roll itself.
        return [(root, [])]

    results: list[tuple[Path, list[str]]] = []

    # Non-KP siblings of KP1 at root level are likely misplaced rolls,
    # but only if they actually contain media — otherwise ignore silently.
    for sib in non_kp1:
        if not _has_media(sib):
            continue
        warn = (
            f"⚠️  '{sib.name}' found alongside known-parent "
            f"'{kp1[0].name}' under '{root.name}' — "
            f"possible misplaced roll. Including in output."
        )
        results.append((sib, [warn]))

    for parent in kp1:
        try:
            children = sorted(
                [d for d in parent.iterdir()
                 if d.is_dir() and not _is_known_sibling(d.name)],
                key=lambda d: d.name,
            )
        except OSError:
            continue

        kp2     = [d for d in children if _is_known_parent(d.name)]
        non_kp2 = [d for d in children if not _is_known_parent(d.name)]

        if kp2:
            # Level-2 known parents — rolls live under them.
            for kp in kp2:
                try:
                    roll_children = sorted(
                        [d for d in kp.iterdir() if d.is_dir()],
                        key=lambda d: d.name,
                    )
                except OSError:
                    continue
                for roll_dir in roll_children:
                    results.append((roll_dir, []))

            # Non-KP2 siblings alongside KP2 dirs are likely misplaced,
            # but only if they actually contain media — otherwise ignore.
            for sib in non_kp2:
                if not _has_media(sib):
                    continue
                warn = (
                    f"⚠️  '{sib.name}' found alongside known-parent "
                    f"'{kp2[0].name}' under '{parent.name}' — "
                    f"possible misplaced roll. Including in output."
                )
                results.append((sib, [warn]))
        else:
            # No KP2 → direct non-KP children are rolls.
            for roll_dir in non_kp2:
                results.append((roll_dir, []))

    return results if results else [(root, [])]


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
        issues.append(Issue(
            anchor=ex_xd,
            message=(
                f"both XDROOT and M4ROOT populated — card was used in "
                f"two Sony cameras or two recording modes (XAVC + "
                f"XAVC-S). Examples: {ex_xd} and {ex_m4}"
            ),
        ))

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
        for sig, group in sorted(
            by_signature.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),  # largest group first
        ):
            example_name = group[0].name
            n = len(group)
            summary_parts.append(
                f"{example_name!r} ({n} clip{'s' if n != 1 else ''})"
            )
        summary = ", ".join(summary_parts)

        # Anchor on the first clip of the smallest group (likely the
        # contaminating intruder, the one the user most needs to see).
        smallest_sig = min(by_signature, key=lambda s: len(by_signature[s]))
        anchor = by_signature[smallest_sig][0].anchor
        issues.append(Issue(
            anchor=anchor,
            message=(
                f"contains clips from multiple recording sessions — "
                f"different filename signatures detected: {summary}. "
                f"Card was likely used in two devices or two sessions "
                f"without reformatting. First example of the minority "
                f"group: {anchor}"
            ),
        ))

    return issues


# ---------------------------------------------------------------------------
# Audio creation-date sorting (ffprobe)
# ---------------------------------------------------------------------------

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
    h  = m.group(4) or "00"
    mi = m.group(5) or "00"
    sec = m.group(6) or "00"
    return f"{y}-{mo}-{d}T{h}:{mi}:{sec}"


def audio_creation_times(wavs: list[Path]) -> dict[Path, str]:
    """Return ``{path: sortable_timestamp}`` for each WAV, via ffprobe.

    Runs a single ``ffprobe`` call across all files (one JSON object per
    file, separated by newlines). Probes BWF/iXML tags stored in the WAV
    format container: ``creation_time``, ``origination_date``
    + ``origination_time``, ``date``. The first usable value wins. Missing or
    unparseable values map to empty strings; the caller falls back to
    ``st_mtime``.
    """
    if not wavs:
        return {}

    # ffprobe can probe multiple files in one invocation; each file
    # produces one JSON object. We separate them with a record separator
    # so we can split reliably even if tag values contain newlines.
    args = [
        "ffprobe", "-v", "quiet",
        "-print_format", f"json",
        "-show_entries", "format_tags=creation_time,origination_date,origination_time,date",
    ]
    # ffprobe doesn't natively batch multiple files into one JSON array,
    # so we call it once per file.
    result: dict[Path, str] = {}
    for p in wavs:
        raw = _run([*args, str(p)])
        ts = ""
        if raw.strip():
            try:
                data = json.loads(raw)
                tags = data.get("format", {}).get("tags", {})
                # Normalise key case — ffprobe lowercases tag names.
                tags = {k.lower(): v for k, v in tags.items()}
                # Priority order: combine origination_date + origination_time
                # (BWF spec fields) first, then creation_time (RF64/iXML).
                orig_date = tags.get("origination_date", "")
                orig_time = tags.get("origination_time", "")
                if orig_date:
                    combined = f"{orig_date} {orig_time}".strip()
                    ts = _normalize_timestamp(combined)
                if not ts:
                    ts = _normalize_timestamp(tags.get("creation_time", ""))
                if not ts:
                    ts = _normalize_timestamp(tags.get("date", ""))
            except (json.JSONDecodeError, AttributeError):
                pass
        result[p] = ts
    return result


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
        from datetime import datetime, timezone
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# First / Last clips
# ---------------------------------------------------------------------------

def first_last_clips(clips: list[Clip]) -> tuple[str, str]:
    """Return ``(first_clip, last_clip)`` for one roll.

    Rules:
      - If any video clips are present, only video clips are considered
        for endpoints — sorted alphabetically by clip name. This avoids
        a sidecar WAV becoming a misleading "last clip".
      - For audio-only rolls of WAV files, sort by creation date because
        alphabetical order can lie.
      - For audio-only rolls containing **any ZAX files** (Zaxcom MARF
        format), sort everything alphabetically. ZAX files are encrypted
        and unreadable by ffprobe, but Zaxcom recorders
        auto-name them sequentially so alphabetical order matches
        recording order. When a roll mixes WAV and ZAX (rare), we
        also fall back to alphabetical to keep the ordering rule
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
    has_zax = any(
        c.files[0].suffix.lower() == ZAX_EXT for c in audios
    )

    if has_zax:
        # Alphabetical works for ZAX (sequential recorder naming) and
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
    The concat-demuxer batching approach was tried previously but proved
    unreliable: ffprobe treats the manifest as one virtual input so
    per-file durations are not individually accessible.

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

    total = 0.0
    for f in video_files:
        raw = _run([
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_entries", "format=duration",
            str(f),
        ])
        if not raw.strip():
            continue
        try:
            dur = json.loads(raw).get("format", {}).get("duration", "")
            if dur:
                total += float(dur)
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue
    return total




def format_hms(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS``, rounded to whole seconds."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Per-roll line assembly
# ---------------------------------------------------------------------------

def render_roll(clips: list[Clip], fields: list[str], roll_root: Path | None = None) -> str:
    """Build the tab-separated value string for one roll (no roll label).

    ``fields`` is an ordered list of field keys to emit, derived from the
    order flags were given on the command line. Valid keys:
    ``"first"``, ``"last"``, ``"n"``, ``"duration"``, ``"size"``.
    ``"roll"`` is handled by the caller since it comes from the roll name.
    ``roll_root`` is the directory to walk for size (matches Finder/Explorer).
    """
    # Compute lazily — only what's needed.
    _first: str | None = None
    _last:  str | None = None

    def get_first_last() -> tuple[str, str]:
        nonlocal _first, _last
        if _first is None:
            _first, _last = first_last_clips(clips)
        return _first, _last

    _duration: float | None = None
    _any_video: bool | None = None

    def get_duration() -> str:
        nonlocal _duration, _any_video
        if _duration is None:
            _duration = total_duration_seconds(clips)
            _any_video = any(c.kind == "video" for c in clips)
        return format_hms(_duration) if _any_video else ""

    out: list[str] = []
    for field in fields:
        if field == "first":
            out.append(get_first_last()[0])
        elif field == "last":
            out.append(get_first_last()[1])
        elif field == "n":
            out.append(str(len(clips)))
        elif field == "duration":
            out.append(get_duration())
        elif field == "size":
            out.append(f"{total_size_gb(clips, roll_root):.2f}")
    return "\t".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

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
    parser.add_argument(
        "-f", "-F", "--first-clip", action="store_true",
        help="print first clip name",
    )
    parser.add_argument(
        "-l", "-L", "--last-clip", action="store_true",
        help="print last clip name.",
    )
    parser.add_argument(
        "-s", "-S", "--size", action="store_true",
        help=f"print total size in {_SIZE_UNIT}",
    )
    parser.add_argument(
        "-d", "-D", "--duration", action="store_true",
        help="print aggregated duration of video clips (HH:MM:SS)",
    )
    parser.add_argument(
        "-c", "-C", "--clip-count", action="store_true",
        help="print the clip count",
    )
    parser.add_argument(
        "-r", "-R", "--roll", action="store_true",
        help="print the roll name",
    )
    parser.add_argument(
        "-E", "--edit-presets",
        action="store_true",
        dest="edit_presets",
        help="edit mrl_presets.toml in your default terminal editor ($EDITOR)",
    )
    parser.add_argument(
        "-O", "--open-presets",
        action="store_true",
        dest="open_presets",
        help="open mrl_presets.toml in the system default app for text files",
    )
    parser.add_argument(
        "--version", action="version", version=f"{__version__}",
    )
    parser.add_argument(
        "paths", nargs="*", default=["."],
        help="camera roll directories (default: current directory)"
    )
    args = parser.parse_args(argv)

    # Open / edit presets and exit — these flags short-circuit everything else.
    if args.open_presets:
        _open_presets_with_default_app()
        return 0

    if args.edit_presets:
        print(f"📋  Presets config file: {PRESETS_PATH}")
        _open_presets_in_editor()
        return 0

    # Build the ordered fields list from the argv token sequence.
    # This drives both render_roll and the header, preserving the exact
    # order the user typed their flags.
    _flag_to_field = {
        "-f": "first", "--first-clip": "first",
        "-l": "last",  "--last-clip":  "last",
        "-s": "size",  "--size":       "size",
        "-d": "duration", "--duration": "duration",
        "-c": "n",     "--clip-count": "n",
        "-r": "roll",  "--roll":        "roll",
    }
    _field_to_header = {
        "first":    "FIRST CLIP",
        "last":     "LAST CLIP",
        "n":        "CLIP COUNT",
        "duration": "DURATION",
        "size":     f"SIZE ({_SIZE_UNIT})",
        "roll":     "ROLL",
    }
    # Default order when no flags given
    _default_fields = ["roll", "first", "last", "n", "duration", "size"]

    raw_argv = argv if argv is not None else sys.argv[1:]
    fields: list[str] = []
    seen: set[str] = set()
    for token in raw_argv:
        # Expand combined short flags like -flsdc into -f -l -s -d -c.
        # Also handle -cl, -cln, etc. Each char after the leading dash
        # is treated as a separate short flag.
        if token.startswith("-") and not token.startswith("--") and len(token) > 2:
            tokens = [f"-{ch}" for ch in token[1:]]
        else:
            tokens = [token]
        for t in tokens:
            f = _flag_to_field.get(t)
            if f and f not in seen:
                fields.append(f)
                seen.add(f)

    if not fields:
        fields = _default_fields[:]


    rolls: list[tuple[str, Path, list[Clip], list[Issue]]] = []

    if len(args.paths) > 1:
        # Multi-path mode: each argument is treated as a named roll
        # directly — no grouping algorithm, insertion order preserved.
        # The user is asserting what the rolls are; we trust the names
        # but still validate contents for mixed-session or other issues.
        for raw_path in args.paths:
            target = Path(raw_path).resolve()
            if not target.is_dir():
                print(f"Error: '{target}' is not a directory.", file=sys.stderr)
                return 1
            clips, issues = find_clips(target)
            roll_issues = list(issues) + validate_roll(clips)
            rolls.append((target.name, target, clips, roll_issues))
    else:
        # Single-path mode (or bare invocation defaulting to "."):
        # use _find_roll_dirs to locate rolls via known-parent detection.
        raw_path = args.paths[0]
        target = Path(raw_path).resolve()
        if not target.is_dir():
            print(f"Error: '{target}' is not a directory.", file=sys.stderr)
            return 1
        for roll_dir, dir_warnings in _find_roll_dirs(target):
            for w in dir_warnings:
                print(w, file=sys.stderr)
            clips, issues = find_clips(roll_dir)
            if not clips and not issues:
                continue
            roll_issues = list(issues) + validate_roll(clips)
            rolls.append((roll_dir.name, roll_dir, clips, roll_issues))

    if not rolls:
        print("Error: no media files found under any input path.", file=sys.stderr)
        return 1

    multi = len(rolls) > 1

    # Print diagnostics for any broken roll to stderr, regardless of
    # single- or multi-roll mode. We do this before stdout so the user
    # sees the warnings first (or at least adjacent) when stderr and
    # stdout flow to the same terminal. For each broken roll we list
    # every issue's message, prefixed with the roll name so it's clear
    # which roll needs investigating.
    had_issues = False
    for name, _root, _clips, issues in rolls:
        if not issues:
            continue
        had_issues = True
        for issue in issues:
            print(
                f"Warning: roll '{name}' — {issue.message}",
                file=sys.stderr,
            )
        print(
            f"Warning: roll '{name}' has issues that need investigation; "
            f"skipping output for this roll. Please check it manually.",
            file=sys.stderr,
        )

    # Drop broken rolls from stdout. Even if -s and -n would technically
    # be unaffected by the issue, emitting any partial value risks the
    # DIT pasting it into the log without realizing the roll is suspect.
    valid_rolls = [(n, root, c) for (n, root, c, i) in rolls if not i]

    if not valid_rolls:
        # Every roll was broken. Stderr already explained why; just
        # exit non-zero so calling shells/scripts notice.
        return 1

    multi_valid = len(valid_rolls) > 1

    want_roll = "roll" in fields
    data_fields = [f for f in fields if f != "roll"]

    # Format a header label: dimmed + underlined first character as flag hint.
    def _fmt_header(label: str) -> str:
        if not label:
            return ""
        return (
            f"{DIM_ERR}{UNDERLINE_ERR}{label[0]}{RESET_ERR}"
            f"{DIM_ERR}{label[1:]}{RESET_ERR}"
        )

    header_labels = [_field_to_header[f] for f in fields]

    def _raw_row(name: str, roll_root: Path, clips: list[Clip]) -> list[str]:
        """Return ordered cell values (strings) for one roll."""
        data_line = render_roll(clips, data_fields, roll_root)
        data_vals = data_line.split("\t") if data_line else []
        di = 0
        cells: list[str] = []
        for f in fields:
            if f == "roll":
                cells.append(name)
            else:
                cells.append(data_vals[di] if di < len(data_vals) else "")
                di += 1
        return cells

    # Collect all rows as cell lists so we can measure column widths.
    all_rows: list[tuple[str, list[str]]] = [
        (name, _raw_row(name, root, clips)) for name, root, clips in valid_rolls
    ]

    # Column widths: max of header label length and any data cell in that column.
    col_widths = [len(h) for h in header_labels]
    for _name, cells in all_rows:
        for i, cell in enumerate(cells):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(cell))

    def _pad_row(cells: list[str], widths: list[int], pad_last: bool = False) -> str:
        """Space-pad each cell to its column width, join with a single tab.

        The last cell is only padded when ``pad_last=True`` — i.e. when a
        courtesy roll label follows and needs to land in an aligned column.
        ``cells`` and ``widths`` must be the same length; a mismatch is a
        bug but we handle it gracefully rather than crashing.
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

        Cells come from ``_raw_row`` which only includes fields the user
        requested — roll is present iff ``-r`` was given. No courtesy
        labels, no trailing spaces, one row per line.
        """
        return "\n".join("\t".join(cells) for _name, cells in rows)

    def _copy_to_clipboard(text: str) -> None:
        """Copy ``text`` to the system clipboard. Silent no-op on failure.

        Tries pyperclip first (handles macOS, Windows, Linux X11/Wayland,
        WSL). Falls back to direct subprocess calls (pbcopy / clip / xclip)
        if pyperclip is not installed.
        """
        try:
            import pyperclip
            pyperclip.copy(text)
            return
        except Exception:
            pass
        # Stdlib fallback.
        try:
            if sys.platform == "darwin":
                subprocess.run(["pbcopy"], input=text, text=True,
                               check=False, **_POPEN_KW)
            elif sys.platform == "win32":
                subprocess.run(["clip"], input=text, text=True,
                               check=False, **_POPEN_KW)
            else:
                for cmd in (["xclip", "-selection", "clipboard"],
                            ["xsel", "--clipboard", "--input"]):
                    r = subprocess.run(cmd, input=text, text=True,
                                       check=False, **_POPEN_KW)
                    if r.returncode == 0:
                        break
        except Exception:
            pass  # Clipboard is best-effort; never crash the script.

    if multi_valid or (multi and valid_rolls):
        # Header to stderr — purely visual, never captured by pipes.
        header_plain = [h for h in header_labels]
        header_padded = [
            h.ljust(col_widths[i]) if i < len(col_widths) - 1 else h
            for i, h in enumerate(header_plain)
        ]
        header_str = "\t".join(_fmt_header(h) for h in header_padded)
        print(header_str, file=sys.stderr)

        for name, cells in all_rows:
            row = _pad_row(cells, col_widths)
            if not want_roll:
                # Courtesy roll label as a trailing dimmed column, excluded
                # from clipboard TSV since it's not in cells.
                row = _pad_row(cells, col_widths, pad_last=True)
                print(f"{row}\t{DIM}({name}){RESET}")
            else:
                print(row)

        _copy_to_clipboard(_clipboard_tsv(all_rows))
        return 1 if had_issues else 0

    # Single valid roll.
    name, cells = all_rows[0]
    row = _pad_row(cells, col_widths)
    if len(fields) > 1:
        header_padded = [
            h.ljust(col_widths[i]) if i < len(col_widths) - 1 else h
            for i, h in enumerate(header_labels)
        ]
        header_str = "\t".join(_fmt_header(h) for h in header_padded)
        print(header_str, file=sys.stderr)
        print(row)
    else:
        print(_fmt_header(header_labels[0]), file=sys.stderr)
        print(row)
    _copy_to_clipboard(_clipboard_tsv(all_rows))
    return 1 if had_issues else 0


if __name__ == "__main__":
    sys.exit(main())