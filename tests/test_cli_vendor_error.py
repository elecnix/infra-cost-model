"""The CLI reports a missing or foreign ``vendors`` package on one line (#289).

Each test puts a ``vendors`` package that isn't this project's first on the
import path, then runs the installed ``infra-cost-model`` script in a
subprocess, so the package is the one the CLI really imports.
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


@pytest.fixture
def foreign_vendors(tmp_path):
    """Return an environment whose import path starts with a foreign ``vendors``."""
    (tmp_path / "vendors").mkdir()
    (tmp_path / "vendors" / "__init__.py").write_text("")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(tmp_path), env.get("PYTHONPATH")) if p
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
def test_foreign_vendors_package_prints_one_error_line(args, foreign_vendors):
    result = subprocess.run(
        [_cli_script(), *args],
        env=foreign_vendors, cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("Error: Vendor prices didn't load")
    assert "pip install -e ." in lines[0]
