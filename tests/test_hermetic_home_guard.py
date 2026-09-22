"""The hermetic HOME is isolated during a run and removed after one.

`conftest.py` creates a temporary directory at import time, because that is
the only point early enough to affect `infra_cost_model.pricing.cache.DB_PATH`
— it reads `Path.home()` when the module loads. The removal is registered with
`atexit`, which runs after `pytest.main` returns.

That placement is the point of these tests. A session fixture's teardown was
the obvious alternative and it leaks: a collection error means no session
fixture ever runs, so its teardown is skipped. The exit paths are checked here
one by one, each in a subprocess, because no test can observe its own exit.
"""

import glob
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def temp_homes() -> set[str]:
    """Every test-home directory the OS temp area currently holds."""
    return set(glob.glob(os.path.join(tempfile.gettempdir(), "test-home-*")))


def run_pytest(args, timeout=180):
    """Run pytest in a subprocess and report its exit code and any leak."""
    before = temp_homes()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout,
    )
    return result, temp_homes() - before


class TestIsolationDuringTheRun:
    """HOME points at the session's temporary directory."""

    def test_home_is_the_isolated_directory(self):
        assert Path(os.environ["HOME"]).name.startswith("test-home-")

    def test_the_catalog_directory_exists_below_it(self):
        """Cache initialization must find its directory, not create one."""
        assert (Path(os.environ["HOME"]) / ".infra-cost-model").is_dir()

    def test_the_catalog_path_resolves_inside_the_temp_home(self):
        """`DB_PATH` lands in the isolate, not the developer's directory.

        `Path.home()` reads HOME, so it agrees with the isolate by
        construction. The check with content is that the module's own
        `DB_PATH` resolves under the isolated directory.
        """
        from infra_cost_model.pricing.cache import DB_PATH

        assert str(DB_PATH).startswith(os.environ["HOME"])


class TestCleanupAfterTheRun:
    """Every way a run can end leaves no directory behind."""

    def test_a_completed_run_removes_its_directory(self, tmp_path):
        probe = tmp_path / "test_probe.py"
        probe.write_text("def test_ok():\n    assert True\n")
        result, leaked = run_pytest([str(probe)])
        assert result.returncode == 0, result.stdout + result.stderr
        assert not leaked, f"a completed run left {sorted(leaked)} behind"

    def test_a_run_failing_during_collection_removes_its_directory(self):
        """No session fixture runs on a collection error.

        A teardown-based cleanup leaks here, which is why the removal is
        registered with `atexit` instead.
        """
        broken = REPO_ROOT / "tests" / "test_zz_broken_collection.py"
        broken.write_text("def test_broken(:\n    pass\n")
        try:
            result, leaked = run_pytest(["tests/test_zz_broken_collection.py"])
        finally:
            broken.unlink()
        assert result.returncode != 0, "the probe file was supposed to be broken"
        assert not leaked, f"a collection failure left {sorted(leaked)} behind"

    def test_an_interrupted_run_removes_its_directory(self):
        """SIGINT mid-session must not leave the directory behind either."""
        slow = REPO_ROOT / "tests" / "test_zz_slow_probe.py"
        slow.write_text(
            "import os, time\n"
            "def test_slow():\n"
            "    print('HOME=' + os.environ['HOME'], flush=True)\n"
            "    time.sleep(10)\n"
        )
        before = temp_homes()
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", "tests/test_zz_slow_probe.py",
             "-q", "-s", "-p", "no:cacheprovider"],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            home = None
            deadline = time.time() + 60
            while time.time() < deadline:
                line = proc.stdout.readline()
                if line.startswith("HOME="):
                    home = line.strip().split("=", 1)[1]
                    break
            assert home, "the probe never reported its HOME"
            # The directory must be alive while the session is still running.
            assert os.path.isdir(home), "HOME was removed mid-session"
            proc.send_signal(signal.SIGINT)
            try:
                proc.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        finally:
            slow.unlink()
        assert not (temp_homes() - before), "an interrupted run left a directory behind"


class TestTheProbeIsHonest:
    """Guard the tests above: they only mean something if isolation ran."""

    def test_a_subprocess_session_gets_an_isolated_home(self, tmp_path):
        probe = tmp_path / "test_probe.py"
        probe.write_text(
            "import os, pathlib\n"
            "def test_isolated():\n"
            "    assert pathlib.Path(os.environ['HOME']).name.startswith('test-home-')\n"
        )
        result, _ = run_pytest([str(probe)])
        assert result.returncode == 0, result.stdout + result.stderr
