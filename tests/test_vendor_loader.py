"""Conformance tests for vendor price loader."""

import tempfile
from pathlib import Path

from infra_cost_model.pricing.cache import PricingCache


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
