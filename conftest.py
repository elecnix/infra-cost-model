"""Hermetic test isolation for infra-cost-model.

Pin HOME to a temporary directory for the entire pytest session so that
tests never read the developer's real ~/.infra-cost-model/pricing.db.
This eliminates the 16 flaky failures caused by catalog hermeticity
(see issue #246, Phase 1).
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# Create an isolated HOME directory before any infra_cost_model modules are imported.
# pytest imports conftest first, so setting HOME here is early enough to affect
# infra_cost_model.pricing.cache.DB_PATH which reads Path.home() at import time.
_test_home = Path(tempfile.mkdtemp(prefix="test-home-"))
atexit.register(shutil.rmtree, _test_home, ignore_errors=True)
os.environ["HOME"] = str(_test_home)

# Ensure the expected directory exists so cache initialization does not error.
(_test_home / ".infra-cost-model").mkdir(parents=True, exist_ok=True)


def pytest_sessionstart(session):
    # Session start hook to document isolation; no further action needed
    # because HOME is already pinned at import time.
    pass


import pytest

@pytest.fixture(scope="session", autouse=True)
def isolated_home():
    """Session-scoped autouse fixture documenting hermetic HOME isolation."""
    # HOME is already pinned to _test_home above. The fixture exists to satisfy
    # the requirement for an explicit session-scoped autouse fixture and to keep
    # the isolation visible in pytest.
    yield
