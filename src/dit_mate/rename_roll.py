#!/usr/bin/env python3

# rename_roll - Batch-rename rolls (or any files/folders) from a TSV dictionary

"""
`rename_roll` reads a two-column TSV file located in the user configuration 
directory. The first column is the current name and the second column is the 
new name, then renames every matching entry inside one or more target directories.

Each row maps one source name to one destination name. Rows starting with
'#' are treated as comments and skipped. Blank lines are also skipped.

Example:

    rename_roll /path/to/day1 /path/to/day1_backup

The script is dry-run safe: pass --dry-run (-n) to preview what would happen
without touching the filesystem.
"""

# Copyright (c) 2026 Luis Gómez Gutiérrez. License: MIT.

import argparse
import importlib.metadata
import os
import sys
from pathlib import Path

from platformdirs import user_config_dir

# ---------------------------------------------------------------------------
# Version & Paths
# ---------------------------------------------------------------------------

__version__ = importlib.metadata.version("dit-mate")

TSV_FILENAME = "rename_dictionary.tsv"
CONFIG_DIR = Path(user_config_dir("dit-mate"))
TSV_PATH = CONFIG_DIR / TSV_FILENAME

# ---------------------------------------------------------------------------
# TSV Lifecycle & Parsing
# ---------------------------------------------------------------------------

def ensure_tsv_exists() -> None:
    """Verify the TSV dictionary exists, or create a blank one if missing."""
    if not TSV_PATH.exists():
        try:
            TSV_PATH.parent.mkdir(parents=True, exist_ok=True)
            TSV_PATH.write_text("", encoding="utf-8")
            print(f"✨  Created new blank dictionary file: {TSV_PATH}")
        except OSError as exc:
            sys.exit(f"❌  Failed to create dictionary file: {exc}")


def load_rename_dict() -> list[tuple[str, str]]:
    """Parse the two-column TSV file into an ordered list of (source, target) pairs.

    Skips blank lines and lines whose first non-whitespace character is '#'.
    Raises SystemExit on any structural error.
    """
    pairs: list[tuple[str, str]] = []
    errors: list[str] = []

    try:
        text = TSV_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"❌  Cannot read TSV file: {exc}")

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 2:
            errors.append(
                f"  Line {lineno}: expected 2 tab-separated columns, "
                f"got {len(cols)}: {raw!r}"
            )
            continue
        src, dst = cols[0].strip(), cols[1].strip()
        if not src:
            errors.append(f"  Line {lineno}: source name is empty")
            continue
        if not dst:
            errors.append(
                f"  Line {lineno}: destination name is empty for source '{src}'"
            )
            continue
        pairs.append((src, dst))

    if errors:
        sys.exit(
            f"❌  Errors in TSV file {TSV_PATH}:\n" + "\n".join(errors)
        )

    if not pairs:
        sys.exit(
            f"❌  TSV file contains no valid rename pairs: {TSV_PATH}\n"
            f"    Run 'rename_roll -E' to add entries to the dictionary."
        )

    return pairs


# ---------------------------------------------------------------------------
# Rename logic
# ---------------------------------------------------------------------------

def rename_in_directory(
    directory: Path,
    pairs: list[tuple[str, str]],
    dry_run: bool,
    verbose: bool,
) -> tuple[list[str], list[str], list[str]]:
    """Apply rename pairs inside *directory* (non-recursive, top-level directories only).

    Returns three lists: renamed, skipped, errors — each as human-readable strings.
    """
    renamed: list[str] = []
    skipped: list[str] = []
    errors:  list[str] = []

    for src_name, dst_name in pairs:
        src_path = directory / src_name
        dst_path = directory / dst_name

        if not src_path.is_dir():
            skipped.append(f"  ⏭   {src_name}  (directory not found in {directory})")
            continue

        if dst_path.exists():
            errors.append(
                f"  ❌  {src_name} → {dst_name}  "
                f"(destination already exists in {directory})"
            )
            continue

        if dry_run:
            renamed.append(f"  🔍  [dry-run] {src_name} → {dst_name}")
        else:
            try:
                src_path.rename(dst_path)
                renamed.append(f"  ✅  {src_name} → {dst_name}")
            except OSError as exc:
                errors.append(f"  ❌  {src_name} → {dst_name}  ({exc})")

    return renamed, skipped, errors


# ---------------------------------------------------------------------------
# CLI helpers (-E / -O open the TSV, mirroring mkday's preset file pattern)
# ---------------------------------------------------------------------------

def open_tsv_with_default_app() -> None:
    """Open the TSV dictionary in the OS default app for text files.

    Uses:
      macOS   → open
      Windows → os.startfile
      Linux   → xdg-open
    """
    ensure_tsv_exists()
    print(f"📋  Opening rename dictionary: {TSV_PATH}")

    try:
        if sys.platform == "darwin":
            os.execvp("open", ["open", str(TSV_PATH)])
        elif sys.platform == "win32":
            os.startfile(str(TSV_PATH))
        else:
            os.execvp("xdg-open", ["xdg-open", str(TSV_PATH)])
    except FileNotFoundError as exc:
        sys.exit(f"❌  Could not open TSV file: {exc}")


def open_tsv_in_editor() -> None:
    """Open the TSV dictionary in the user's preferred terminal editor.

    Editor resolution order:
      1. $EDITOR environment variable
      2. $VISUAL environment variable
      3. nano  (macOS / Linux fallback)
      4. notepad  (Windows fallback)
    """
    ensure_tsv_exists()
    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or ("notepad" if sys.platform == "win32" else "nano")
    )

    print(f"📝  Opening {TSV_PATH} with '{editor}'…")
    try:
        os.execvp(editor, [editor, str(TSV_PATH)])
    except FileNotFoundError:
        sys.exit(
            f"❌  Editor not found: '{editor}'\n"
            f"    Set the $EDITOR environment variable to your preferred editor,\n"
            f"    or use -O to open the TSV with the system default app."
        )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rename_roll",
        description="Batch-rename rolls from a configuration TSV dictionary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "directories",
        nargs="*",
        metavar="PATH",
        help=(
            "One or more directories to apply the renames in. "
            "If omitted, defaults to the current working directory."
        ),
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        dest="dry_run",
        help="preview what would be renamed without touching the filesystem",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="also print skipped entries (not found in directory)",
    )
    parser.add_argument(
        "-E", "--edit-dict",
        action="store_true",
        dest="edit_dict",
        help="edit the TSV dictionary in your default terminal editor ($EDITOR)",
    )
    parser.add_argument(
        "-O", "--open-dict",
        action="store_true",
        dest="open_dict",
        help="open the TSV dictionary in the system default app for text files",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{__version__}",
    )
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.open_dict:
        open_tsv_with_default_app()
        return

    if args.edit_dict:
        print(f"📋  Rename dictionary: {TSV_PATH}")
        open_tsv_in_editor()
        return

    # Check and parse file for execution runs
    ensure_tsv_exists()
    if not TSV_PATH.is_file():
        sys.exit(f"❌  Not a file: {TSV_PATH}")

    pairs = load_rename_dict()

    # Resolve target directories
    if args.directories:
        directories = []
        for raw in args.directories:
            p = Path(raw).resolve()
            if not p.exists():
                sys.exit(f"❌  Directory does not exist: {p}")
            if not p.is_dir():
                sys.exit(f"❌  Not a directory: {p}")
            if not args.dry_run and not os.access(p, os.W_OK):
                sys.exit(f"❌  Directory is not writable: {p}")
            directories.append(p)
    else:
        try:
            cwd = Path.cwd().resolve()
        except FileNotFoundError:
            sys.exit(
                "❌  Current directory does not exist. "
                "Please change to a valid directory."
            )
        if not cwd.is_dir():
            sys.exit(f"❌  Current directory does not exist: {cwd}")
        if not args.dry_run and not os.access(cwd, os.W_OK):
            sys.exit(f"❌  Current directory is not writable: {cwd}")
        directories = [cwd]

    mode_label = "[DRY RUN] " if args.dry_run else ""
    print(f"\n🎬  rename_roll {mode_label}— {len(pairs)} pairs from {TSV_PATH.name}\n")

    total_renamed = total_skipped = total_errors = 0

    for directory in directories:
        print(f"📂  {directory}")
        renamed, skipped, errors = rename_in_directory(
            directory, pairs, dry_run=args.dry_run, verbose=args.verbose
        )

        for line in renamed:
            print(line)
        if args.verbose:
            for line in skipped:
                print(line)
        for line in errors:
            print(line)

        total_renamed += len(renamed)
        total_skipped += len(skipped)
        total_errors  += len(errors)

    # Summary
    verb = "Would rename" if args.dry_run else "Renamed"
    print(
        f"\n{'─' * 48}\n"
        f"  {verb}:  {total_renamed}\n"
        f"  Skipped: {total_skipped}  (not found)\n"
        f"  Errors:  {total_errors}\n"
    )

    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    sys.exit(main())