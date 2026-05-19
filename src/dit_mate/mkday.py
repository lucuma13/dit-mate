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
import os
import re
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from platformdirs import user_config_dir

# ---------------------------------------------------------------------------
# Preset definitions
# ---------------------------------------------------------------------------

# Version is imported from dit-mate
__version__ = importlib.metadata.version("dit-mate")


PRESETS_FILENAME = "mkday_presets.toml"
DEFAULT_PRESETS  = "mkday_default_presets.toml"
CONFIG_DIR       = Path(user_config_dir("dit-mate"))
PRESETS_PATH     = CONFIG_DIR / PRESETS_FILENAME
BUNDLED_PRESETS  = Path(__file__).parent / "data" / DEFAULT_PRESETS

REQUIRED_KEYS = {
    "aliases", "prefix_path", "day_folder_format", "day_padding", "subfolders"
}


def load_presets() -> dict:
    """Load and validate presets from the user config TOML file.

    On first run, if the file does not exist, it is seeded from the
    bundled mkday_presets.toml that ships with the package.
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
        with open(PRESETS_PATH, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(
            f"❌  Could not parse presets config file: {PRESETS_PATH}\n"
            f"    {exc}\n"
            f"    Run 'mkday -e' to edit the file and fix the error."
        )

    errors = []
    for preset_key, cfg in data.items():
        missing = REQUIRED_KEYS - set(cfg.keys())
        if missing:
            errors.append(
                f"  Preset '{preset_key}' is missing required keys: "
                f"  {', '.join(sorted(missing))}"
                f"  Run 'mkday -e' to edit the file and fix the error."
            )
        if "day_padding" in cfg:
            val = cfg["day_padding"]
            if not (isinstance(val, int) and val >= 0):
                errors.append(
                    f"  Preset '{preset_key}': day_padding must be a positive integer"
                    f"  Run 'mkday -e' to edit the file and fix the error."
                )
        if "subfolders" in cfg and not isinstance(cfg["subfolders"], list):
            errors.append(
                f"  Preset '{preset_key}': subfolders must be a list with quoted values.\n\n"
                f"  E.g.\n"
                f"  subfolders = [\"AUDIO\", \"CAMERA\", \"DOCS\", \"PROXY\", \"STILLS\"]\n"
                f"  Run 'mkday -e' to edit the file and fix the error."
            )
        if "aliases" in cfg and not isinstance(cfg["aliases"], list):
            errors.append(
                f"  Preset '{preset_key}': aliases must be a list with quoted values.\n\n"
                f"  Example:\n"
                f"  aliases = [\"example_alias1\", \"example_alias2\"]\n"
                f"  Run 'mkday -e' to edit the file and fix the error."
            )

    if errors:
        sys.exit(
            f"❌  Invalid presets in {PRESETS_PATH}:\n"
            + "\n".join(errors)
            + "\n    Run 'mkday -e' to edit the file and fix the errors."
        )

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
        for candidate in [preset_key, *cfg["aliases"]]
    }
    if key not in lookup:
        valid = sorted(lookup.keys())
        raise ValueError(
            f"Unknown preset '{raw}'. Valid values: {', '.join(valid)}"
        )
    return lookup[key]


def build_day_folder_name(preset_cfg: dict, day: int) -> str:
    """Build the day folder name from preset config and day number."""
    today = datetime.today().strftime("%Y%m%d")
    padding = preset_cfg["day_padding"]
    day_str = str(day) if padding == 0 else str(day).zfill(padding)
    return preset_cfg["day_folder_format"].format(today=today, day=day_str)


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
    day_indent    = indent * depth if depth > 0 else indent[:-4]  # flush left when no prefix
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
        "-d", "--day",
        required=False,
        metavar="N",
        help="shooting day number",
    )
    parser.add_argument(
        "-p", "--production-preset",
        required=False,
        metavar="PRESET",
        dest="preset",
        help="preset name",
    )
    parser.add_argument(
        "-E", "--edit-presets",
        action="store_true",
        dest="edit_presets",
        help="edit mkday_presets.toml in your default editor ($EDITOR)",
    )
    parser.add_argument(
        "-O", "--open-presets",
        action="store_true",
        dest="open_presets",
        help=(
            "open mkday_presets.toml in the system default app for text files"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def open_presets_with_default_app() -> None:
    """Open mkday_presets.toml in the OS default app for .toml files.

    Uses:
      macOS   → open
      Windows → os.startfile
      Linux   → xdg-open
    """
    if not PRESETS_PATH.exists():
        sys.exit(f"❌  Presets config file not found: {PRESETS_PATH}")

    print(f"📋  Open presets config file: {PRESETS_PATH}")

    try:
        if sys.platform == "darwin":
            os.execvp("open", ["open", str(PRESETS_PATH)])
        elif sys.platform == "win32":
            os.startfile(str(PRESETS_PATH))
        else:
            os.execvp("xdg-open", ["xdg-open", str(PRESETS_PATH)])
    except FileNotFoundError as exc:
        sys.exit(f"❌  Could not open presets file: {exc}")


def open_presets_in_editor() -> None:
    """Open mkday_presets.py in the user's preferred editor.

    Editor resolution order:
      1. $EDITOR environment variable
      2. $VISUAL environment variable
      3. nano  (macOS / Linux fallback)
      4. notepad  (Windows fallback)
    """
    presets_file = PRESETS_PATH
    if not presets_file.exists():
        sys.exit(f"❌  Presets config file not found: {presets_file}")

    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or ("notepad" if sys.platform == "win32" else "nano")
    )

    print(f"📝  Opening {presets_file} with '{editor}'…")
    try:
        os.execvp(editor, [editor, str(presets_file)])
    except FileNotFoundError:
        sys.exit(
            f"❌  Editor not found: '{editor}'\n"
            f"    Set the $EDITOR environment variable to your preferred editor,"
            f"    or use -O for opening mkday_presets.toml with the system default app"
        )


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

    # -d and -p are required for normal operation
    if not args.day:
        parser.error("the following arguments are required: -d/--day")
    if not args.preset:
        parser.error("the following arguments are required: -p/--production-preset")

    # Resolve preset
    try:
        preset_key = resolve_preset(args.preset)
    except ValueError as exc:
        parser.error(str(exc))

    preset_cfg   = PRESETS[preset_key]

    # Resolve destination directories
    if args.outputs:
        destinations = []
        for raw in args.outputs:
            p = Path(raw).resolve()
            if not p.exists():
                sys.exit(f"❌  Destination does not exist: {p}")
            if not os.access(p, os.W_OK):
                sys.exit(f"❌  Destination is not writable: {p}")
            destinations.append(p)
    else:
        try:
            cwd = Path.cwd().resolve()
        except FileNotFoundError:
            sys.exit("❌  Current directory does not exist. Please change to a valid directory.")
        if not cwd.exists():
            sys.exit(f"❌  Current directory does not exist: {cwd}")
        if not os.access(cwd, os.W_OK):
            sys.exit(f"❌  Current directory is not writable: {cwd}")
        destinations = [cwd]

    # Build day folder
    day_folder_name = build_day_folder_name(preset_cfg, args.day)
    subfolders      = preset_cfg["subfolders"]

    # Check if day folder exists in the destinations
    for dest in destinations:
        base_path, _ = resolve_base_path(dest, preset_cfg)
        day_path = base_path / day_folder_name
        if day_path.exists():
            sys.exit(
                f"❗  Day folder already exists: {day_path}\n"
                f"❌  Make day folder structure aborted"
            )

    # Create day folder in all destinations, collect warnings
    warnings = []
    created_path = None
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

    print_tree(created_path, subfolders, preset_cfg.get("prefix_path"))


if __name__ == "__main__":
    sys.exit(main())