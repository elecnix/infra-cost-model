"""The hermetic HOME is isolated during a run and removed after one.

`conftest.py` creates a temporary directory at import time, because that is
the only point early enough to affect `infra_cost_model.pricing.cache.DB_PATH`
— it reads `Path.home()` when the module loads. The removal is registered with
`atexit`, which runs after `pytest.main` returns.

That placement is the point of these tests. A session fixture's teardown was
the obvious alternative and it leaks: a collection error means no session
fixture ever runs, so its teardown is skipped. The exit paths are checked here
one by one, each in a subprocess, because no test can observe its own exit.

Each subprocess gets its own probe files and its own temp folder under
`tmp_path`, so pytest runs going on at the same time, in the same checkout or
elsewhere on the machine, can't change the result (#328).
"""

import glob
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def probe_dir():
    """A folder of its own for one test's probe files.

    It sits below the repo root, so a probe run picks up the root
    `conftest.py`. A probe outside the repo would not load it and would pass
    by inheriting this session's HOME. The dot at the start of the name keeps
    `pytest tests/` from collecting the probes, and the unique name keeps
    runs in the same checkout from overwriting or deleting each other's
    probes.
    """
    folder = Path(tempfile.mkdtemp(prefix=".hermetic-probe-", dir=REPO_ROOT))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def temp_homes(folder) -> set[str]:
    """Every test-home directory that `folder` holds."""
    return set(glob.glob(os.path.join(folder, "test-home-*")))


def private_temp_env(tmp_path) -> tuple[Path, dict[str, str]]:
    """A temp folder for one subprocess, and an environment that points at it.

    `conftest.py` makes the test home with `tempfile.mkdtemp`, which uses
    `TMPDIR`. A subprocess with its own `TMPDIR` puts its test home there, so
    a leak check on that folder can't see another pytest run's directories
    in the shared temp folder (#328).
    """
    folder = tmp_path / "subprocess-tmp"
    folder.mkdir()
    return folder, {**os.environ, "TMPDIR": str(folder)}


def run_pytest(args, tmp_path, timeout=180):
    """Run pytest in a subprocess and report its exit code and any leak."""
    folder, env = private_temp_env(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *args, "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout,
    )
    return result, temp_homes(folder)


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

    def test_a_completed_run_removes_its_directory(self, tmp_path, probe_dir):
        probe = probe_dir / "test_probe.py"
        probe.write_text("def test_ok():\n    assert True\n")
        result, leaked = run_pytest([str(probe)], tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not leaked, f"a completed run left {sorted(leaked)} behind"

    def test_a_run_failing_during_collection_removes_its_directory(self, tmp_path, probe_dir):
        """No session fixture runs on a collection error.

        A teardown-based cleanup leaks here, which is why the removal is
        registered with `atexit` instead.
        """
        broken = probe_dir / "test_broken_collection.py"
        broken.write_text("def test_broken(:\n    pass\n")
        result, leaked = run_pytest([str(broken)], tmp_path)
        assert result.returncode != 0, "the probe file was supposed to be broken"
        assert not leaked, f"a collection failure left {sorted(leaked)} behind"

    def test_an_interrupted_run_removes_its_directory(self, tmp_path, probe_dir):
        """SIGINT mid-session must not leave the directory behind either."""
        slow = probe_dir / "test_slow_probe.py"
        slow.write_text(
            "import os, time\n"
            "def test_slow():\n"
            "    print('HOME=' + os.environ['HOME'], flush=True)\n"
            "    time.sleep(10)\n"
        )
        folder, env = private_temp_env(tmp_path)
        proc = subprocess.Popen(
            [sys.executable, "-m", "pytest", str(slow),
             "-q", "-s", "-p", "no:cacheprovider"],
            cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
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
            assert Path(home).parent == folder, "the probe ignored its TMPDIR"
            # The directory must be alive while the session is still running.
            assert os.path.isdir(home), "HOME was removed mid-session"
            proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        assert not temp_homes(folder), "an interrupted run left a directory behind"


class TestTheLeakCheckIgnoresOtherRuns:
    """A concurrent pytest run on the machine is not a leak (#328)."""

    def test_a_directory_made_elsewhere_during_the_run_is_not_reported(
        self, tmp_path, probe_dir
    ):
        """Stand in for another run by making a directory mid-subprocess.

        The probe creates a `test-home-*` directory in this process's temp
        folder, the one a separate pytest run on the machine would use.
        """
        shared = tempfile.gettempdir()
        probe = probe_dir / "test_probe.py"
        probe.write_text(
            "import tempfile\n"
            "def test_other_run():\n"
            f"    tempfile.mkdtemp(prefix='test-home-other-run-', dir={shared!r})\n"
        )
        try:
            result, leaked = run_pytest([str(probe)], tmp_path)
        finally:
            for other in glob.glob(os.path.join(shared, "test-home-other-run-*")):
                os.rmdir(other)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not leaked, f"another run's directory was reported: {sorted(leaked)}"


class TestTheProbeIsHonest:
    """Guard the tests above: they only mean something if isolation ran."""

    def test_a_subprocess_session_gets_an_isolated_home(self, tmp_path, probe_dir):
        """The probe's HOME is a new test home in the probe's own TMPDIR.

        Checking the name alone isn't enough: a probe that never loaded
        `conftest.py` inherits this session's HOME, which has the same prefix.
        """
        probe = probe_dir / "test_probe.py"
        probe.write_text(
            "import os, pathlib, tempfile\n"
            "def test_isolated():\n"
            "    home = pathlib.Path(os.environ['HOME'])\n"
            "    assert home.name.startswith('test-home-')\n"
            "    assert home.parent == pathlib.Path(tempfile.gettempdir())\n"
        )
        result, _ = run_pytest([str(probe)], tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
