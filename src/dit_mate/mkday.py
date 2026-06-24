#!/usr/bin/env python3

# mkday - Make new shooting day folder structure

"""
`makeday` creates a user-defined folder structure in the volumes provided,
a time-saver at the beginning of each shooting day.

Example:

Make the folder structure for shooting day 1, using the preset "example",
on two backup drives:

```bash
mkday -p example -d 1 "path/to/drive1" "path/to/drive2"
```
"""
# Copyright (c) 2026 Luis Gómez Gutiérrez. License: MIT.

import argparse
import importlib.metadata
import re
import sys
from datetime import datetime
from pathlib import Path

from dit_mate._internal.config import CONFIG_DIR, bundled_data, load_or_seed_toml
from dit_mate._internal.openers import open_in_default_app, open_in_editor
from dit_mate._internal.paths import resolve_target_dirs

# -----------------------------------------------------------------------------
# Version
# -----------------------------------------------------------------------------

try:
    __version__ = importlib.metadata.version("dit-mate")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"

# ---------------------------------------------------------------------------
# Preset definitions
# ---------------------------------------------------------------------------

PRESETS_FILENAME = "mkday_presets.toml"
DEFAULT_PRESETS = "mkday_default_presets.toml"
PRESETS_PATH = CONFIG_DIR / PRESETS_FILENAME

REQUIRED_KEYS = {"day_folder_format"}

DEFAULTS = {
    "aliases": [],
    "prefix_path": "",
    "day_padding": 0,
    "subfolders": [],
}


def _validate_preset(preset_key: str, cfg: dict) -> list[str]:
    """Return human-readable validation errors for one preset (empty if valid).

    Checks required keys and the types of the optional keys (``day_padding``,
    ``subfolders``, ``aliases``) when present.
    """
    errors: list[str] = []
    missing = REQUIRED_KEYS - set(cfg.keys())
    if missing:
        errors.append(
            f"  Preset '{preset_key}' is missing required keys: "
            f"  {', '.join(sorted(missing))}"
            f"  Run 'mkday -E' to edit the file and fix the error."
        )
    if "day_padding" in cfg:
        val = cfg["day_padding"]
        if not (isinstance(val, int) and val >= 0):
            errors.append(
                f"  Preset '{preset_key}': day_padding must be a non-negative integer"
                f"  Run 'mkday -E' to edit the file and fix the error."
            )
    if "subfolders" in cfg and not isinstance(cfg["subfolders"], list):
        errors.append(
            f"  Preset '{preset_key}': subfolders must be a list with quoted values.\n\n"
            f"  E.g.\n"
            f'  subfolders = ["AUDIO", "CAMERA", "DOCS", "PROXY", "STILLS"]\n'
            f"  Run 'mkday -E' to edit the file and fix the error."
        )
    if "aliases" in cfg and not isinstance(cfg["aliases"], list):
        errors.append(
            f"  Preset '{preset_key}': aliases must be a list with quoted values.\n\n"
            f"  Example:\n"
            f'  aliases = ["example_alias1", "example_alias2"]\n'
            f"  Run 'mkday -E' to edit the file and fix the error."
        )
    return errors


def load_presets() -> dict:
    """Load and validate presets from the user config TOML file.

    On first run, if the file does not exist, it is seeded from the
    bundled mkday_presets.toml that ships with the package.
    """
    data = load_or_seed_toml(PRESETS_PATH, bundled_data(DEFAULT_PRESETS))

    errors = []
    for preset_key, cfg in data.items():
        errors.extend(_validate_preset(preset_key, cfg))

    if errors:
        sys.exit(
            f"❌  Invalid presets in {PRESETS_PATH}:\n"
            + "\n".join(errors)
            + "\n    Run 'mkday -E' to edit the file and fix the errors."
        )

    # Apply defaults for any omitted optional keys
    for cfg in data.values():
        for key, default in DEFAULTS.items():
            cfg.setdefault(key, default)

    return data


PRESETS = load_presets()

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def resolve_preset(raw: str) -> str:
    """Return the canonical preset key or raise ValueError.

    Builds a lookup from each preset's own 'aliases' list,
    so aliases live alongside the rest of the preset config in PRESETS.
    """
    key = raw.strip().lower()
    lookup = {
        candidate: preset_key
        for preset_key, cfg in PRESETS.items()
        for candidate in [preset_key, *cfg.get("aliases", [])]
    }
    if key not in lookup:
        valid = sorted(lookup.keys())
        raise ValueError(f"Unknown preset '{raw}'. Valid values: {', '.join(valid)}")
    return lookup[key]


def _date_tokens(now: datetime) -> dict[str, str]:
    """Return a map of date token → value for the given datetime.

    D and M (non-padded) use platform-specific strftime flags:
      Linux / macOS → %-d, %-m
      Windows       → %#d, %#m
    """
    no_pad = "#" if sys.platform == "win32" else "-"
    return {
        "YYYY": now.strftime("%Y"),
        "YY": now.strftime("%y"),
        "MM": now.strftime("%m"),
        "DD": now.strftime("%d"),
        "M": now.strftime(f"%{no_pad}m"),
        "D": now.strftime(f"%{no_pad}d"),
    }


def _expand_date_tokens(fmt: str, now: datetime) -> str:
    """Expand date letter-sequences inside any {…} group.

    Tokens are replaced longest-first so YYYY is never partially matched by YY.
    Groups with no recognised date tokens (e.g. {day}) are left untouched.

    Examples (2026-05-21):
      {YYYYMMDD}    → 20260521
      {YYYY-MM-DD}  → 2026-05-21
      {YY/MM/DD}    → 26/05/21
      {D/M/YYYY}    → 21/5/2026
    """
    tokens = _date_tokens(now)

    def replacer(m: re.Match) -> str:
        inner = m.group(1)
        result = inner
        for token, value in sorted(tokens.items(), key=lambda x: -len(x[0])):
            result = result.replace(token, value)
        # Only substitute if something actually changed; otherwise leave {…} intact
        return result if result != inner else m.group(0)

    return re.sub(r"\{([^}]+)\}", replacer, fmt)


def build_day_folder_name(preset_cfg: dict, day: int | None) -> str:
    """Build the day folder name from preset config and day number.

    Date tokens can be combined freely inside a single {…} group or kept
    separate — punctuation between letters is preserved as-is:

      {YYYYMMDD}_DAY{day}       → 20260521_DAY03
      {YYYY-MM-DD}_DAY{day}     → 2026-05-21_DAY03
      {D/M/YYYY}_DAY{day}       → 21/5/2026_DAY03
      {YY}{MM}{DD}_{day}        → 260521_3   (day_padding = 0)
      {YYYYMMDD}                → 20260521    (no {day} token, day arg unused)

    Tokens:
      YYYY  four-digit year        MM  zero-padded month (01-12)
      YY    two-digit year         DD  zero-padded day   (01-31)
                                   M   non-padded month  (1-12)
                                   D   non-padded day    (1-31)
      {day} is the shooting day number, padded per day_padding.
            Omit {day} from the format if the preset does not use day numbers.
    """
    now = datetime.now().astimezone()
    expanded = _expand_date_tokens(preset_cfg["day_folder_format"], now)

    if "{day}" not in expanded:
        return expanded

    padding = preset_cfg["day_padding"]
    day_str = str(day) if padding == 0 else str(day).zfill(padding)
    return expanded.format(day=day_str)


def resolve_base_path(cwd: Path, preset_cfg: dict) -> tuple[Path, bool]:
    """
    Return (base_path, prefix_already_existed) where base_path is the
    directory directly above the day folder.

    prefix_path strings in presets may use either / or \\ as separators
    (depending on which OS the preset was authored on). We normalise by
    splitting on both before reassembling with Path(), so the result is
    always correct on macOS, Linux, and Windows.
    """
    raw_prefix = preset_cfg.get("prefix_path")
    if not raw_prefix:
        return cwd, False

    # Split on both separator styles, drop empty parts
    parts = [p for p in re.split(r"[/\\]", raw_prefix) if p]
    prefix_path = cwd / Path(*parts)
    already_existed = prefix_path.exists()
    return prefix_path, already_existed


def create_structure(base_path: Path, day_folder_name: str, subfolders: list[str]) -> Path:
    """
    Create the day folder and its subfolders under base_path.
    Returns the Path of the created day folder.
    """
    day_path = base_path / day_folder_name

    # Create each subfolder (parents=True handles base_path + day folder)
    for sub in subfolders:
        target = day_path / sub
        target.mkdir(parents=True, exist_ok=True)

    return day_path


def print_tree(day_path: Path, subfolders: list[str], prefix_path: str | None) -> None:
    """Print a tree of the created structure."""
    print("\n✅  Created folder structure:")

    # Prefix
    segments = [s for s in re.split(r"[/\\\\]", prefix_path or "") if s]
    indent = "    "
    for i, segment in enumerate(segments):
        connector = "└──" if i > 0 else ""
        if connector:
            print(f"{indent * i}{connector} 📁 {segment}")
        else:
            print(f"{indent}📁 {segment}")

    # Day folder row
    depth = len(segments)
    day_connector = "└──" if depth > 0 else ""
    if day_connector:
        print(f"{indent * depth}{day_connector} 📁 {day_path.name}")
    else:
        print(f"    📁 {day_path.name}")

    # Subfolders
    sub_depth = depth + 1
    for i, sub in enumerate(subfolders):
        connector = "└──" if i == len(subfolders) - 1 else "├──"
        print(f"{indent * sub_depth}{connector} 📁 {sub}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mkday",
        description="Make new shooting day folder structure.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "outputs",
        nargs="*",
        metavar="PATH",
        help=(
            "One or more destination directories where the day folder structure "
            "will be created. If omitted, defaults to the current working directory."
        ),
    )
    parser.add_argument(
        "-d",
        "--day",
        required=False,
        metavar="N",
        help="shooting day number",
    )
    parser.add_argument(
        "-p",
        "--production-preset",
        required=False,
        metavar="PRESET",
        dest="preset",
        help="preset name",
    )
    parser.add_argument(
        "-E",
        "--edit-presets",
        action="store_true",
        dest="edit_presets",
        help="edit mkday_presets.toml in your default editor ($EDITOR)",
    )
    parser.add_argument(
        "-O",
        "--open-presets",
        action="store_true",
        dest="open_presets",
        help=("open mkday_presets.toml in the system default app for text files"),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def open_presets_with_default_app() -> None:
    """Open mkday_presets.toml in the OS default app for .toml files."""
    if not PRESETS_PATH.exists():
        sys.exit(f"❌  Presets config file not found: {PRESETS_PATH}")
    open_in_default_app(PRESETS_PATH, label="presets config file")


def open_presets_in_editor() -> None:
    """Open mkday_presets.toml in the user's preferred editor ($EDITOR/$VISUAL)."""
    if not PRESETS_PATH.exists():
        sys.exit(f"❌  Presets config file not found: {PRESETS_PATH}")
    open_in_editor(PRESETS_PATH, label="presets config file")


def _create_day_folders(
    destinations: list[Path],
    preset_cfg: dict,
    preset_key: str,
    day_folder_name: str,
    subfolders: list[str],
) -> Path:
    """Create the day-folder structure in every destination; return the created path.

    Pre-flight aborts (``sys.exit``) if the day folder already exists in any
    destination, so a partial run can't happen. Prints a warning for any
    destination where the preset's prefix folders already existed, and exits
    on a permission/filesystem error.
    """
    for dest in destinations:
        base_path, _ = resolve_base_path(dest, preset_cfg)
        day_path = base_path / day_folder_name
        if day_path.exists():
            sys.exit(f"❗  Day folder already exists: {day_path}\n❌  Make day folder structure aborted")

    warnings: list[str] = []
    created_path: Path | None = None
    for dest in destinations:
        base_path, prefix_already_existed = resolve_base_path(dest, preset_cfg)
        if prefix_already_existed:
            warnings.append(f"\n⚠️   Folder structure for {preset_key} already exists at {dest}")
        try:
            created_path = create_structure(base_path, day_folder_name, subfolders)
        except PermissionError as exc:
            sys.exit(f"❌  Permission denied: {exc}")
        except OSError as exc:
            sys.exit(f"❌  OS error: {exc}")

    for warning in warnings:
        print(warning)

    if created_path is None:
        # Unreachable: resolve_target_dirs always yields ≥1 destination, so the
        # loop above ran. Guard keeps the return type honest (Path, not None).
        sys.exit("❌  No destinations to create the day folder in.")
    return created_path


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Open presets in OS default app and exit
    if args.open_presets:
        open_presets_with_default_app()
        return

    # Edit presets in terminal editor and exit
    if args.edit_presets:
        print(f"📋  Presets config file: {PRESETS_PATH}")
        open_presets_in_editor()
        return

    # -p is always required for normal operation
    if not args.preset:
        parser.error("the following arguments are required: -p/--production-preset")

    # Resolve preset
    try:
        preset_key = resolve_preset(args.preset)
    except ValueError as exc:
        parser.error(str(exc))

    preset_cfg = PRESETS[preset_key]

    # -d is only required if the preset's day_folder_format uses {day}
    uses_day = "{day}" in preset_cfg["day_folder_format"]
    if uses_day and not args.day:
        parser.error(f"preset '{preset_key}' requires a day number: -d/--day N")
    if not uses_day and args.day:
        print(f"⚠️   Preset '{preset_key}' does not use {{day}} in its folder format (-d ignored)")

    # Resolve destination directories
    destinations = resolve_target_dirs(args.outputs, check_writable=True)

    # Build day folder
    day_folder_name = build_day_folder_name(preset_cfg, args.day)
    subfolders = preset_cfg["subfolders"]

    created_path = _create_day_folders(destinations, preset_cfg, preset_key, day_folder_name, subfolders)

    print_tree(created_path, subfolders, preset_cfg.get("prefix_path"))


if __name__ == "__main__":
    sys.exit(main())
