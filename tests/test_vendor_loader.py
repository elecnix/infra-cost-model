"""Conformance tests for vendor price loader."""

import shutil
import sqlite3
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

import pytest

from infra_cost_model.pricing.cache import Price, PricingCache
from infra_cost_model.pricing.vendors import load_vendor_prices


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
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    source_dir = tmp_path / "source"
    shutil.copytree(
        Path(__file__).parents[1],
        source_dir,
        ignore=shutil.ignore_patterns(".git", "build", "*.egg-info", "__pycache__"),
    )
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", str(wheel_dir)],
        check=True,
        cwd=source_dir,
        capture_output=True,
        text=True,
    )
    install_dir = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_dir),
            str(next(wheel_dir.glob("*.whl"))),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
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

    source_vendors = resources.files("vendors")
    broken_vendors = tmp_path / "vendors"
    shutil.copytree(source_vendors, broken_vendors)
    (broken_vendors / "auth0" / "prices.yaml").write_text("- vendor: auth0\n  service: Auth0\n")

    original_files = resources.files
    monkeypatch.setattr(
        resources,
        "files",
        lambda package: broken_vendors if package == "vendors" else original_files(package),
    )

    with pytest.raises(ValueError, match=r"vendors/auth0/prices.yaml, row 1: 'usage_metric'"):
        load_vendor_prices(cache)

    with sqlite3.connect(cache.db_path) as conn:
        assert conn.execute(
            "SELECT price_usd FROM prices WHERE vendor = 'sentinel' AND source = 'vendor'"
        ).fetchone() == (7.0,)
