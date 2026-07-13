"""Test suite for the shared terminal colour helpers."""

from types import SimpleNamespace
from typing import TextIO, cast

import pytest

from dit_mate._internal import term


@pytest.mark.parametrize(
    ("env", "tty", "expected"),
    [
        ({}, True, True),
        ({}, False, False),
        ({"NO_COLOR": "1"}, True, False),
        ({"FORCE_COLOR": "1"}, False, True),
        ({"NO_COLOR": "1", "FORCE_COLOR": "1"}, True, False),  # NO_COLOR wins
        ({"NO_COLOR": ""}, True, True),  # the convention: only a non-empty value counts
    ],
)
def test_supports_color_follows_no_color_and_force_color(monkeypatch, env, tty, expected):
    """
    no-color.org: NO_COLOR disables, FORCE_COLOR forces, otherwise the TTY
    decides.
    """
    for var in ("NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setattr(term, "enable_ansi_on_windows", lambda: True)
    stream = cast(TextIO, SimpleNamespace(isatty=lambda: tty))  # only isatty() is used
    assert term.supports_color(stream) is expected
