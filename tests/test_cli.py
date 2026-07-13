"""Test suite for the dit-mate suite entry point."""

import tomllib
from pathlib import Path

import pytest

from dit_mate import cli

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


# ---------------------------------------------------------------------------
# Tool listing
# ---------------------------------------------------------------------------


def test_tools_match_declared_entry_points():
    """Every tool listed is a real command, and every command is listed."""
    scripts = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["scripts"]
    listed = {name for name, _ in cli.TOOLS}
    assert listed == set(scripts) - {"dit-mate"}


def test_format_tools_aligns_and_colours():
    plain = cli.format_tools(color=False)
    coloured = cli.format_tools(color=True)

    assert "\033[" not in plain
    assert "\033[" in coloured
    for name, summary in cli.TOOLS:
        assert name in plain
        assert summary in plain
    # Summaries all start at the same column.
    starts = {line.index(summary) for (_, summary), line in zip(cli.TOOLS, plain.splitlines(), strict=True)}
    assert len(starts) == 1


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


def test_bare_invocation_prints_the_tool_list(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["dit-mate"])
    cli.main()
    out = capsys.readouterr().out
    for name, summary in cli.TOOLS:
        assert name in out
        assert summary in out


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_flags_print_the_tool_list(capsys, monkeypatch, flag):
    monkeypatch.setattr("sys.argv", ["dit-mate", flag])
    cli.main()
    out = capsys.readouterr().out
    assert "tools:" in out
    assert all(name in out for name, _ in cli.TOOLS)


def test_help_screen_has_no_usage_or_options_boilerplate(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["dit-mate"])
    cli.main()
    out = capsys.readouterr().out
    assert "usage:" not in out
    assert "options:" not in out


def test_version_flag_prints_the_package_version(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["dit-mate", "--version"])
    cli.main()
    # Bare version number, matching the rest of the suite.
    assert capsys.readouterr().out.strip() == cli.__version__


def test_unknown_argument_shows_help_but_exits_nonzero(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["dit-mate", "basicmeta"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "tools:" in out
    assert all(name in out for name, _ in cli.TOOLS)
    assert "❌" not in out
