#!/usr/bin/env python3

# dit-mate - Suite entry point

"""
`dit-mate` lists the tools in the suite with a brief description of each.

Every tool is its own command; this entry point is a signpost, not a
dispatcher. Run a tool with `--help` to see its own options.

Example:

```bash
dit-mate
```
"""
# Copyright (c) 2026 Luis Gómez Gutiérrez. License: MIT.

import importlib.metadata
import sys

from dit_mate._internal.term import DIM, RESET, supports_color
from dit_mate.update_checker import run_with_update_check

# -----------------------------------------------------------------------------
# Version
# -----------------------------------------------------------------------------

try:
    __version__ = importlib.metadata.version("dit-mate")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"

# -----------------------------------------------------------------------------
# The suite
# -----------------------------------------------------------------------------

DESCRIPTION = "A toolkit for Digital Imaging Technicians and Media Managers."
FOOTER = "Run any tool with --help for its own options."

TOOLS: tuple[tuple[str, str], ...] = (
    ("basicmeta", "List frame rate, resolution and recorded date of original camera files."),
    ("mkday", "Create a shooting day folder structure on one or more volumes."),
    ("mrl", "Copy Master Rushes Log values to the clipboard."),
    ("xpandroll", "Batch-rename camera rolls from a configuration TSV dictionary."),
    ("lifsaver", "Force-mount stalled 'Untitled' cards on macOS (LIFS bug workaround)."),
)

HELP_FLAGS = frozenset({"-h", "--help"})


def format_tools(*, color: bool) -> str:
    """Return the tool list as an aligned, optionally coloured block."""
    width = max(len(name) for name, _ in TOOLS)
    lines = [
        f"  {name:<{width}}  {DIM}{summary}{RESET}" if color else f"  {name:<{width}}  {summary}"
        for name, summary in TOOLS
    ]
    return "\n".join(lines)


def format_help(*, color: bool) -> str:
    """Return the full help screen: what the suite is, then what's in it."""
    return f"{DESCRIPTION}\n\ntools:\n{format_tools(color=color)}\n\n{FOOTER}"


def _main() -> None:
    # No arguments to parse beyond two flags, and nothing to dispatch to — argparse
    # would only add back the usage/options boilerplate we don't want on this screen.
    args = sys.argv[1:]

    if args == ["--version"]:
        print(__version__)
        return

    print(format_help(color=supports_color(sys.stdout)))

    # An unknown argument gets the same help screen — there is nothing more useful to
    # say — but exits non-zero so scripts and shells still see it as a failure.
    if args and not HELP_FLAGS.issuperset(args):
        sys.exit(1)


def main() -> None:
    run_with_update_check("dit-mate", __version__, _main)
