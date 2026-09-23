"""The CLI reports missing vendor price data on one line (#289).

Each test runs the installed ``infra-cost-model`` script in a subprocess. A
``sitecustomize`` module on the import path makes
``resources.files("infra_cost_model.vendors")`` return an empty directory, as
an install that left out the price files would (#290).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = str(REPO_ROOT / "examples" / "github-copilot.yaml")


def _cli_script() -> str:
    script = Path(sys.executable).parent / "infra-cost-model"
    if script.exists():
        return str(script)
    found = shutil.which("infra-cost-model")
    if found is None:
        pytest.skip("the infra-cost-model script is not installed")
    return found


_SITECUSTOMIZE = """
from importlib import resources
from pathlib import Path

_EMPTY = Path({empty!r})
_original_files = resources.files


def _files(package):
    if package == "infra_cost_model.vendors":
        return _EMPTY
    return _original_files(package)


resources.files = _files
"""


@pytest.fixture
def missing_vendor_data(tmp_path):
    """Return an environment where the vendor package has no price files."""
    empty = tmp_path / "empty_vendors"
    empty.mkdir()
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE.format(empty=str(empty)))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(hook_dir), env.get("PYTHONPATH")) if p
    )
    return env


@pytest.mark.parametrize("args", [
    ["compute", EXAMPLE],
    ["whatif", EXAMPLE, "--parameter", "frequency", "--value", "2", "--catalog"],
    ["what-if", EXAMPLE, "--param", "frequency", "--values", "1,2", "--catalog"],
    ["sensitivity", EXAMPLE, "--parameter", "frequency", "--catalog"],
    ["sync-pricing"],
    ["seed-pricing"],
], ids=lambda args: args[0])
def test_missing_vendor_data_prints_one_error_line(args, missing_vendor_data):
    result = subprocess.run(
        [_cli_script(), *args],
        env=missing_vendor_data, cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("Error: Vendor prices didn't load")
    assert "pip install -e ." in lines[0]
