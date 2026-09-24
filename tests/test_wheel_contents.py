"""The built wheel contains every data file the package reads at run time (#265).

A test run from the source tree finds every file whatever the packaging says,
so this test builds a wheel and reads its file list. The `wheel-install` CI job
goes further: it installs the wheel into a clean virtual environment and runs
the command-line tool.
"""

import email
import email.message
import fnmatch
import zipfile

import pytest
from _wheel import REPO_ROOT, build_wheel

# The directories setuptools packages. Every JSON and YAML file under them is
# data some code path loads: the schema, the seed prices, the vendor rows.
PACKAGED_DIRS = ("infra_cost_model",)


def _data_files() -> set[str]:
    """JSON and YAML files under the packaged directories, as wheel paths."""
    files = set()
    for top in PACKAGED_DIRS:
        for path in (REPO_ROOT / top).rglob("*"):
            if path.suffix in {".json", ".yaml", ".yml"} and "__pycache__" not in path.parts:
                files.add(path.relative_to(REPO_ROOT).as_posix())
    return files


@pytest.fixture(scope="module")
def wheel_path(tmp_path_factory):
    return build_wheel(tmp_path_factory.mktemp("wheel"))


@pytest.fixture(scope="module")
def wheel_names(wheel_path) -> set[str]:
    with zipfile.ZipFile(wheel_path) as zf:
        return set(zf.namelist())


@pytest.fixture(scope="module")
def wheel_metadata(wheel_path) -> email.message.Message:
    """The wheel's METADATA file, parsed as the email-style headers it holds."""
    with zipfile.ZipFile(wheel_path) as zf:
        (name,) = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        return email.message_from_bytes(zf.read(name))


def test_data_files_found():
    """The source tree has data files, so an empty check can't pass by accident."""
    files = _data_files()
    assert "infra_cost_model/schema/cost-model.schema.json" in files
    assert "infra_cost_model/pricing/seed/seed_prices.json" in files
    assert "infra_cost_model/vendors/github-copilot/prices.yaml" in files


def test_wheel_contains_every_data_file(wheel_names):
    missing = sorted(_data_files() - wheel_names)
    assert not missing, f"the wheel leaves out data files: {missing}"


def test_wheel_puts_vendor_data_inside_the_package(wheel_names):
    """Vendor rows ship under ``infra_cost_model/vendors/``, not a top-level ``vendors`` (#290)."""
    prices = [n for n in wheel_names if fnmatch.fnmatch(n, "infra_cost_model/vendors/*/prices.yaml")]
    assert prices, "the wheel has no infra_cost_model/vendors/*/prices.yaml"
    assert "infra_cost_model/vendors/__init__.py" in wheel_names
    top_level = sorted(n for n in wheel_names if n.startswith("vendors/"))
    assert not top_level, f"the wheel still installs a top-level vendors package: {top_level}"


def test_wheel_declares_the_mit_license(wheel_metadata):
    """PyPI shows the license from the SPDX expression in METADATA (#243)."""
    assert wheel_metadata["License-Expression"] == "MIT"
    assert wheel_metadata.get_all("License-File") == ["LICENSE"]


def test_wheel_ships_the_license_file(wheel_names):
    licenses = [n for n in wheel_names if n.endswith(".dist-info/licenses/LICENSE")]
    assert licenses, "the wheel has no .dist-info/licenses/LICENSE"


def test_wheel_metadata_links_to_the_repository(wheel_metadata):
    urls = wheel_metadata.get_all("Project-URL") or []
    assert "Repository, https://github.com/elecnix/infra-cost-model" in urls
    assert wheel_metadata["Description-Content-Type"] == "text/markdown"
