"""Point HOME at a scratch directory before anything imports app.

app binds OUTPUT_DIR, CONFIG_FILE and LOG_DIR from ``Path.home()`` at import
time, so a test run that imports app under the real HOME reads and writes the
user's live ~/.config/video-generator/config.yaml. Individual test modules each
redirect HOME themselves, but that only holds if the module that happens to
import app first is one of them — and two modules use
``os.environ.setdefault("HOME", ...)``, which is a no-op in a shell where HOME
is already set. pytest loads this conftest before collecting any test module,
which is the only point early enough to make the redirect unconditional.
"""
import os
import tempfile
from pathlib import Path

import pytest

_REAL_HOME = Path.home()

os.environ["HOME"] = tempfile.mkdtemp(prefix="spielbot-test-home-")


def pytest_sessionstart(session):
    """Fail the run rather than let it write the real config directory."""
    if Path.home() == _REAL_HOME:
        raise pytest.UsageError(
            f"HOME is still {_REAL_HOME} — tests would write the real "
            f"~/.config/video-generator/config.yaml. Aborting."
        )
