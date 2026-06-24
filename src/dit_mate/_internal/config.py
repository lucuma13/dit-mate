"""Per-user config paths and TOML preset loading — shared across the CLIs."""

import shutil
import sys
import tomllib
from pathlib import Path

from platformdirs import user_config_dir

#: Per-user config directory shared by every dit-mate tool.
CONFIG_DIR = Path(user_config_dir("dit-mate"))

#: Directory of default config files bundled inside the installed package.
#: ``__file__`` lives in ``dit_mate/_internal/``, so the data dir is two
#: levels up — computing it here keeps every caller from getting it wrong.
_DATA_DIR = Path(__file__).parent.parent / "data"


def bundled_data(filename: str) -> Path:
    """Return the path to *filename* under the package's bundled ``data/`` dir."""
    return _DATA_DIR / filename


def load_or_seed_toml(path: Path, bundled: Path) -> dict:
    """Load the TOML config at *path*, seeding it from *bundled* on first run.

    Creates the config directory and copies the bundled default when *path*
    doesn't exist yet, so user customisations survive package updates. Exits
    with a clear message if the bundled default is missing or the file can't
    be parsed. Returns the parsed mapping; callers do their own validation.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not bundled.exists():
            sys.exit(f"❌  Bundled defaults not found: {bundled}\n    Re-installing dit-mate should fix this.")
        shutil.copy(bundled, path)

    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"❌  Could not parse config file: {path}\n    {exc}\n    Use -E to edit the file and fix the error.")
