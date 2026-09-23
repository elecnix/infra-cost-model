"""Conformance tests for vendor price loader."""

import importlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

import pytest
import yaml
from _wheel import build_wheel, unpack_wheel

from infra_cost_model.pricing.cache import Price, PricingCache
from infra_cost_model.pricing.vendors import (
    VendorPackageError,
    _read_vendor_prices,
    load_vendor_prices,
)

VENDORS_PACKAGE = "infra_cost_model.vendors"


def test_vendor_loader_loads_github_copilot_and_skips_template():
    # Use a temporary DB to avoid polluting the real cache
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "pricing.db"
        cache = PricingCache(db_path=db_path, seed=True)

        # Verify vendor rows are present
        source_info = cache.source_info()
        assert "vendor" in source_info, "Vendor source not loaded"
        vendor_rows = source_info["vendor"]
        assert vendor_rows > 0, "No vendor rows loaded"

        # Query the seat row
        seat_price = cache.query(
            vendor="github",
            service="Copilot",
            region="global",
            usage_metric="Copilot-Seat-Month",
        )
        assert seat_price is not None, "Github Copilot seat price not found"
        assert seat_price.price_usd == 19.00
        assert seat_price.per == "seats", "per field did not survive round-trip"

        # Query the credit tiered rows
        credit_price = cache.query(
            vendor="github",
            service="Copilot",
            region="global",
            usage_metric="Copilot-Credit",
        )
        assert credit_price is not None, "Github Copilot credit price not found"
        # Should be a TieredPrice with two tiers
        from infra_cost_model.pricing.cache import TieredPrice
        assert isinstance(credit_price, TieredPrice), "Credit pricing should be tiered"
        assert len(credit_price.tiers) == 2

        # Ensure template directory is not loaded
        # Template would have vendor "example-vendor" if loaded; check it is absent
        example = cache.query(
            vendor="example",
            service="Example",
            region="global",
            usage_metric="Example-Metric",
        )
        assert example is None, "Template rows should not be loaded"


def test_vendor_data_loads_from_an_installed_wheel(tmp_path):
    """Bundled prices work without access to the repository checkout."""
    wheel = build_wheel(tmp_path)
    install_dir = tmp_path / "installed"
    unpack_wheel(wheel, install_dir)
    script = """
from pathlib import Path
from infra_cost_model.pricing.cache import PricingCache
cache = PricingCache(db_path=Path.cwd() / 'pricing.db')
price = cache.query(vendor='github', service='Copilot', region='global', usage_metric='Copilot-Seat-Month')
assert price is not None
assert price.price_usd == 19.0
"""
    isolated_dir = tmp_path / "outside-source"
    isolated_dir.mkdir()
    subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(install_dir)!r});\n{script}"],
        check=True,
        cwd=isolated_dir,
        capture_output=True,
        text=True,
    )


def test_vendor_loader_preserves_rows_when_validation_fails(monkeypatch, tmp_path):
    cache = PricingCache(db_path=tmp_path / "pricing.db")
    sentinel = Price(
        vendor="sentinel",
        service="Sentinel",
        region="global",
        product_family=None,
        attributes={},
        usage_metric="Sentinel-Metric",
        unit="requests",
        price_usd=7.0,
        source="vendor",
        fetched_at="now",
    )
    cache.upsert(sentinel)

    source_vendors = resources.files(VENDORS_PACKAGE)
    broken_vendors = tmp_path / "vendors"
    shutil.copytree(source_vendors, broken_vendors)
    (broken_vendors / "auth0" / "prices.yaml").write_text("- vendor: auth0\n  service: Auth0\n")

    original_files = resources.files
    monkeypatch.setattr(
        resources,
        "files",
        lambda package: broken_vendors if package == VENDORS_PACKAGE else original_files(package),
    )

    with pytest.raises(ValueError, match=r"infra_cost_model/vendors/auth0/prices.yaml, row 1: 'usage_metric'"):
        load_vendor_prices(cache)

    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute(
            "SELECT price_usd FROM prices WHERE vendor = 'sentinel' AND source = 'vendor'"
        ).fetchone() == (7.0,)


def _replace_vendors_package(monkeypatch, result):
    """Make ``resources.files(VENDORS_PACKAGE)`` return *result*, or raise it."""
    original_files = resources.files

    def fake_files(package):
        if package != VENDORS_PACKAGE:
            return original_files(package)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(resources, "files", fake_files)


def test_vendor_data_lives_inside_the_package():
    """The data is in ``infra_cost_model.vendors``, a name no other project can install (#290)."""
    package = importlib.import_module(VENDORS_PACKAGE)
    root = resources.files(package)
    prices_files = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith("_") and entry.joinpath("prices.yaml").is_file()
    )
    assert "github-copilot" in prices_files

    rows_on_disk = sum(
        len(yaml.safe_load(root.joinpath(name, "prices.yaml").read_text(encoding="utf-8")))
        for name in prices_files
    )
    assert len(_read_vendor_prices()) == rows_on_disk


@pytest.mark.parametrize(
    "failure",
    [ModuleNotFoundError(f"No module named '{VENDORS_PACKAGE}'"), TypeError("not a package")],
)
def test_missing_vendors_package_is_an_error(monkeypatch, tmp_path, failure):
    """A broken install must not price every vendor node at $0 without a word."""
    _replace_vendors_package(monkeypatch, failure)

    with pytest.raises(VendorPackageError, match=r"pip install -e \.") as excinfo:
        PricingCache(db_path=tmp_path / "pricing.db")
    assert f"can't import the '{VENDORS_PACKAGE}' package" in str(excinfo.value)


def test_vendors_package_without_data_is_an_error(monkeypatch, tmp_path):
    """An install that left out the price files is an error, not a $0 estimate."""
    empty = tmp_path / "site-packages" / "infra_cost_model" / "vendors"
    empty.mkdir(parents=True)
    (empty / "__init__.py").write_text("")
    _replace_vendors_package(monkeypatch, empty)

    with pytest.raises(VendorPackageError, match=r"has no vendor price files") as excinfo:
        PricingCache(db_path=tmp_path / "pricing.db")
    assert str(empty) in str(excinfo.value)
    assert "pip install -e ." in str(excinfo.value)
