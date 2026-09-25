"""Keep broad lifecycle campaigns out of the interactive Windows app host."""
import os
from pathlib import Path

import pytest


def pytest_configure(config):
    if os.name != "nt" or os.environ.get("OMNI_DISPOSABLE_TEST_HOST") == "1":
        return
    # Explicit test files/node IDs remain available for reviewed checks.
    # A separate disposable host can opt into the directory-wide campaign.
    if any(Path(str(arg).split("::", 1)[0]).is_dir() for arg in config.args):
        raise pytest.UsageError(
            "Broad Omni Studio tests are disabled on the interactive Windows host. "
            "Choose reviewed test files. Use OMNI_DISPOSABLE_TEST_HOST=1 only in a "
            "disposable test host for the full lifecycle campaign."
        )
