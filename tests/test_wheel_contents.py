"""The built wheel contains every data file the package reads at run time (#265).

A test run from the source tree finds every file whatever the packaging says,
so this test builds a wheel and reads its file list. The `wheel-install` CI job
goes further: it installs the wheel into a clean virtual environment and runs
the command-line tool.
"""

import zipfile

import pytest
from _wheel import REPO_ROOT, build_wheel

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
    wheel = build_wheel(tmp_path_factory.mktemp("wheel"))
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
