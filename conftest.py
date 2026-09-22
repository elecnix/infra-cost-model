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

import pytest

# Create an isolated HOME directory before any infra_cost_model modules are imported.
# pytest imports conftest first, so setting HOME here is early enough to affect
# infra_cost_model.pricing.cache.DB_PATH which reads Path.home() at import time.
_test_home = Path(tempfile.mkdtemp(prefix="test-home-"))
os.environ["HOME"] = str(_test_home)

# Ensure the expected directory exists so cache initialization does not error.
(_test_home / ".infra-cost-model").mkdir(parents=True, exist_ok=True)

# Remove the directory when the interpreter exits.
#
# This runs after `pytest.main` returns, so the directory outlives the session
# and no test can lose its HOME mid-run. It also covers the exit paths a
# session fixture misses: a collection error means no session fixture ever
# runs, and a session fixture's teardown is skipped when the session aborts.
# `ignore_errors` keeps an undeletable file from turning a run's exit code
# into a failure. Measured on three paths — a normal run, a run failing during
# collection, and a SIGINT mid-session — nothing is left behind.
atexit.register(shutil.rmtree, _test_home, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def isolated_home():
    """Keep the isolation visible to anyone reading a test run.

    HOME is pinned at import time, before this fixture can run, because that
    is the only point early enough to affect `DB_PATH`. The fixture exists so
    the behavior is discoverable from a test file, and it asserts the thing it
    documents rather than standing empty.
    """
    assert os.environ["HOME"] == str(_test_home)
    assert Path(os.environ["HOME"]).name.startswith("test-home-")
    yield
