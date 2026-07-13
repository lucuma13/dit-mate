#!/usr/bin/env python3

# xpandroll - Batch-rename roll directories from a TSV dictionary

"""
`xpandroll` reads a two-column TSV file located in the user configuration
directory. The first column is the current name and the second column is the
new name, then renames every matching directory (top-level only, files are
ignored) inside one or more target directories.

Each row maps one source name to one destination name. Rows starting with
'#' are treated as comments and skipped. Blank lines are also skipped.

Example:

    xpandroll /path/to/day1 /path/to/day1_backup

The script is dry-run safe: pass --dry-run (-n) to preview what would happen
without touching the filesystem.
"""

# Copyright (c) 2026 Luis Gómez Gutiérrez. License: MIT.

import argparse
import importlib.metadata
import sys
from pathlib import Path

from dit_mate._internal.config import CONFIG_DIR
from dit_mate._internal.openers import maybe_open_config
from dit_mate._internal.utils import resolve_target_dirs
from dit_mate.update_checker import run_with_update_check

# -----------------------------------------------------------------------------
# Version
# -----------------------------------------------------------------------------

try:
    __version__ = importlib.metadata.version("dit-mate")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TSV_FILENAME = "rename_dictionary.tsv"
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


def _validate_pair(src: str, dst: str, lineno: int, seen_src: dict[str, int], seen_dst: dict[str, int]) -> str | None:
    """Return an error message for one (src, dst) pair, or None if it's valid.

    Duplicate sources/destinations make the result order-dependent
    (the second rename fails or clobbers) — reject them up front.
    """
    if not src:
        return f"  Line {lineno}: source name is empty"
    if not dst:
        return f"  Line {lineno}: destination name is empty for source '{src}'"
    if src in seen_src:
        return f"  Line {lineno}: duplicate source '{src}' (already used on line {seen_src[src]})"
    if dst in seen_dst:
        return f"  Line {lineno}: duplicate destination '{dst}' (already used on line {seen_dst[dst]})"
    return None


def load_rename_dict() -> list[tuple[str, str]]:
    """Parse the two-column TSV file into an ordered list of (source, target) pairs.

    Skips blank lines and lines whose first non-whitespace character is '#'.
    Raises SystemExit on any structural error.
    """
    EXPECTED_COLS = 2  # source<TAB>destination

    pairs: list[tuple[str, str]] = []
    errors: list[str] = []
    seen_src: dict[str, int] = {}
    seen_dst: dict[str, int] = {}

    try:
        text = TSV_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"❌  Cannot read TSV file: {exc}")

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < EXPECTED_COLS:
            errors.append(f"  Line {lineno}: expected 2 tab-separated columns, got {len(cols)}: {raw!r}")
            continue
        src, dst = cols[0].strip(), cols[1].strip()
        error = _validate_pair(src, dst, lineno, seen_src, seen_dst)
        if error:
            errors.append(error)
            continue
        seen_src[src] = lineno
        seen_dst[dst] = lineno
        pairs.append((src, dst))

    if errors:
        sys.exit(f"❌  Errors in TSV file {TSV_PATH}:\n" + "\n".join(errors))

    if not pairs:
        sys.exit(
            f"❌  TSV file contains no valid rename pairs: {TSV_PATH}\n"
            f"    Run 'xpandroll -E' to add entries to the dictionary."
        )

    return pairs


# ---------------------------------------------------------------------------
# Rename logic
# ---------------------------------------------------------------------------


def _is_case_only_rename(src_path: Path, dst_path: Path, src_name: str, dst_name: str) -> bool:
    """True when src and dst are the same directory entry differing only in case.

    On case-insensitive filesystems (macOS APFS/HFS+ defaults, Windows)
    ``dst_path.exists()`` matches the source itself for a case-only rename
    like ``roll_a → ROLL_A``. That rename is valid — ``os.rename`` handles
    it in place — so it must not be reported as a destination collision.
    """
    if src_name == dst_name:
        return False
    try:
        return src_path.samefile(dst_path)
    except OSError:
        return False


def rename_in_directory(
    directory: Path,
    pairs: list[tuple[str, str]],
    dry_run: bool,
) -> tuple[list[str], list[str], list[str]]:
    """Apply rename pairs inside *directory* (non-recursive, top-level directories only).

    Returns three lists: renamed, skipped, errors — each as human-readable strings.
    """
    renamed: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for src_name, dst_name in pairs:
        src_path = directory / src_name
        dst_path = directory / dst_name

        if not src_path.is_dir():
            skipped.append(f"  ⏭   {src_name}  (directory not found in {directory})")
            continue

        if dst_path.exists() and not _is_case_only_rename(src_path, dst_path, src_name, dst_name):
            errors.append(f"  ❌  {src_name} → {dst_name}  (destination already exists in {directory})")
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
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xpandroll",
        description="Batch-rename rolls from a configuration TSV dictionary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "directories",
        nargs="*",
        metavar="PATH",
        help=(
            "One or more directories to apply the renames in. If omitted, defaults to the current working directory."
        ),
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="preview what would be renamed without touching the filesystem",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also print skipped entries (not found in directory)",
    )
    parser.add_argument(
        "-E",
        "--edit-dict",
        action="store_true",
        dest="edit_dict",
        help="edit the TSV dictionary in your default terminal editor ($EDITOR)",
    )
    parser.add_argument(
        "-O",
        "--open-dict",
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


def _main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # -O/-E: open the dictionary in the default app or $EDITOR, then exit.
    # ensure_tsv_exists creates a blank dictionary on first use.
    if maybe_open_config(
        TSV_PATH, open_app=args.open_dict, edit=args.edit_dict, label="rename dictionary", ensure=ensure_tsv_exists
    ):
        return

    # Check and parse file for execution runs
    ensure_tsv_exists()
    if not TSV_PATH.is_file():
        sys.exit(f"❌  Not a file: {TSV_PATH}")

    pairs = load_rename_dict()

    # Resolve target directories
    directories = resolve_target_dirs(args.directories, check_writable=not args.dry_run)

    mode_label = "[DRY RUN] " if args.dry_run else ""
    print(f"\n🎬  xpandroll {mode_label}— {len(pairs)} pairs from {TSV_PATH.name}\n")

    total_renamed = total_skipped = total_errors = 0

    for directory in directories:
        print(f"📂  {directory}")
        renamed, skipped, errors = rename_in_directory(directory, pairs, dry_run=args.dry_run)

        for line in renamed:
            print(line)
        if args.verbose:
            for line in skipped:
                print(line)
        for line in errors:
            print(line)

        total_renamed += len(renamed)
        total_skipped += len(skipped)
        total_errors += len(errors)

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


def main() -> None:
    run_with_update_check("dit-mate", __version__, _main)


if __name__ == "__main__":
    sys.exit(main())
