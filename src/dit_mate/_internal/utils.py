"""Small shared helpers."""

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class FieldOrderAction(argparse.Action):
    """store_true that also records the order field flags were typed.

    Column order in basicmeta/mrl output follows the order the field flags
    appear on the command line. Recording that order inside the argparse
    action (rather than re-scanning raw argv) means prefix abbreviations
    (``--res``), combined short flags (``-flsdc``), and every registered
    spelling of a flag are all handled by argparse itself — the action fires
    once per occurrence, whatever the spelling.

    Usage::

        parser.set_defaults(field_order=[])
        parser.add_argument("--fps", action=FieldOrderAction, field="fps", ...)

    After parsing, ``namespace.field_order`` holds the field keys in the
    order first seen (empty list when no field flags were given).
    """

    def __init__(self, option_strings: Sequence[str], dest: str, field: str, help: str | None = None) -> None:  # noqa: A002
        super().__init__(option_strings, dest, nargs=0, default=False, help=help)
        self.field = field

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        setattr(namespace, self.dest, True)
        order = getattr(namespace, "field_order", None)
        if order is None:
            order = []
            namespace.field_order = order
        if self.field not in order:
            order.append(self.field)


def resolve_target_dirs(raw_paths: list[str] | None, *, check_writable: bool) -> list[Path]:
    """Resolve explicit directory args, or fall back to the cwd, validating each.

    Each path must exist and be a directory; with ``check_writable`` it must
    also be writable. Exits with a message on the first failure. Returns the
    resolved, validated directories.
    """
    if raw_paths:
        out: list[Path] = []
        for raw in raw_paths:
            p = Path(raw).resolve()
            if not p.exists():
                sys.exit(f"❌  Directory does not exist: {p}")
            if not p.is_dir():
                sys.exit(f"❌  Not a directory: {p}")
            if check_writable and not os.access(p, os.W_OK):
                sys.exit(f"❌  Directory is not writable: {p}")
            out.append(p)
        return out

    try:
        cwd = Path.cwd().resolve()
    except FileNotFoundError:
        sys.exit("❌  Current directory does not exist. Please change to a valid directory.")
    if not cwd.is_dir():
        sys.exit(f"❌  Current directory does not exist: {cwd}")
    if check_writable and not os.access(cwd, os.W_OK):
        sys.exit(f"❌  Current directory is not writable: {cwd}")
    return [cwd]
