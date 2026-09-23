"""The built wheel contains every data file the package reads at run time (#265).

A test run from the source tree finds every file whatever the packaging says,
so this test builds a wheel and reads its file list. The `wheel-install` CI job
goes further: it installs the wheel into a clean virtual environment and runs
the command-line tool.
"""

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The directories setuptools packages. Every JSON and YAML file under them is
# data some code path loads: the schema, the seed prices, the vendor rows.
PACKAGED_DIRS = ("infra_cost_model", "vendors")


def _data_files() -> set[str]:
    """JSON and YAML files under the packaged directories, as wheel paths."""
    files = set()
    for top in PACKAGED_DIRS:
        for path in (REPO_ROOT / top).rglob("*"):
            if path.suffix in {".json", ".yaml", ".yml"} and "__pycache__" not in path.parts:
                files.add(path.relative_to(REPO_ROOT).as_posix())
    return files


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory) -> set[str]:
    # Build from a fresh copy. setuptools reuses a `build/` directory left in
    # the source tree, and stale files there can put a file in the wheel that
    # the packaging config no longer includes.
    src = tmp_path_factory.mktemp("src") / "repo"
    shutil.copytree(
        REPO_ROOT,
        src,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "node_modules", "build", "__pycache__", "*.egg-info"
        ),
    )
    out = tmp_path_factory.mktemp("wheel")
    cmd = [sys.executable, "-m", "pip", "wheel", "--no-deps", "--quiet", "-w", str(out), str(src)]
    # Reuse the running interpreter's setuptools when it has one, so the test
    # doesn't need network access to build.
    if importlib.util.find_spec("setuptools") is not None:
        cmd.insert(4, "--no-build-isolation")
    subprocess.run(cmd, check=True, cwd=out)
    (wheel,) = out.glob("infra_cost_model-*.whl")
    with zipfile.ZipFile(wheel) as zf:
        return set(zf.namelist())


def test_data_files_found():
    """The source tree has data files, so an empty check can't pass by accident."""
    files = _data_files()
    assert "infra_cost_model/schema/cost-model.schema.json" in files
    assert "infra_cost_model/pricing/seed/aws_pricelist_seed.json" in files
    assert any(f.startswith("vendors/") for f in files)


def test_wheel_contains_every_data_file(wheel_names):
    missing = sorted(_data_files() - wheel_names)
    assert not missing, f"the wheel leaves out data files: {missing}"
