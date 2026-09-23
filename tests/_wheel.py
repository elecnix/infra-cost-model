"""Build and unpack a wheel of this repository for the packaging tests (#279).

The tests build with `pip` when the running interpreter can import it. A
virtual environment made by `uv venv` has no `pip`, so the tests then build
with `uv`. With neither tool, they skip: the `wheel-install` CI job still
builds and installs the wheel on every PR.
"""

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _importable(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def wheel_build_command(src: Path, out: Path) -> list[str]:
    """Return the command that builds a wheel of `src` into `out`.

    Calls `pytest.skip` when neither `pip` nor `uv` is available. It never
    skips when `pip` is importable, so a packaging failure can't hide there.
    """
    # Reuse the running interpreter's setuptools when it has one, so the
    # build doesn't need network access.
    reuse_setuptools = _importable("setuptools")
    if _importable("pip"):
        cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet"]
        if reuse_setuptools:
            cmd.append("--no-build-isolation")
        return [*cmd, "-w", str(out), str(src)]
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "build", "--wheel", "--quiet", "--out-dir", str(out)]
        if reuse_setuptools:
            cmd += ["--no-build-isolation", "--python", sys.executable]
        return [*cmd, str(src)]
    pytest.skip("can't build a wheel: pip and uv are both missing")


def build_wheel(tmp_dir: Path) -> Path:
    """Build a wheel from a fresh copy of the repository and return its path.

    setuptools reuses a `build/` directory left in the source tree, and stale
    files there can put a file in the wheel that the packaging config no
    longer includes. A fresh copy leaves them out.
    """
    src = tmp_dir / "src"
    # `.hermetic-probe-*` folders belong to test_hermetic_home_guard.py runs
    # going on at the same time. One can vanish mid-copy, and the copy fails.
    shutil.copytree(
        REPO_ROOT,
        src,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "node_modules", "build", "dist", "__pycache__", "*.egg-info",
            ".hermetic-probe-*",
        ),
    )
    out = tmp_dir / "dist"
    out.mkdir()
    subprocess.run(
        wheel_build_command(src, out), check=True, cwd=out, capture_output=True, text=True
    )
    (wheel,) = out.glob("infra_cost_model-*.whl")
    return wheel


def unpack_wheel(wheel: Path, target: Path) -> None:
    """Install a pure-Python wheel into `target`, as `pip install --target` does.

    A pure-Python wheel installs by unzipping it, so this needs no installer.
    """
    assert wheel.name.endswith("-none-any.whl"), f"not a pure-Python wheel: {wheel.name}"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(target)
