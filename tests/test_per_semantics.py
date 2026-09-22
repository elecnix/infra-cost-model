"""What `per` means, pinned against the rows that use it.

`per` names the cost model parameter that a row's tier boundaries are stated
in terms of. `per: seats` on a boundary of 1,900 means "1,900 per seat", so a
25-seat customer has 47,500 included. The quantity a caller passes stays in
the row's own unit; the multiplier only converts boundaries.

The alternative reading, that `per` converts the row's price too, contradicts
the shipped rows. GitHub Copilot's seat row states `price_usd: 19.00` with
`per: seats`, and 25 seats costs $475. Dividing by the multiplier would make
it $19, which is not what a seat costs.

What `per` must not do is swallow usage. A row whose parameter is the same
unit as its own quantity turns a real bill into $0, because the boundary
becomes the parameter multiplied by the quantity. Clerk's M2M row carried
`per: tokens` on a token quantity and priced every use at $0.
"""

import pytest

from infra_cost_model.pricing.cache import Price, TieredPrice
from infra_cost_model.pricing.catalog import PricingCatalog, _CostResult


def row(price_usd, start=None, end=None, per=None,
        metric="requests", unit="requests"):
    return Price(
        vendor="v", service="s", region="global", product_family=None,
        attributes={}, usage_metric=metric, unit=unit,
        price_usd=price_usd, start_usage_amount=start,
        end_usage_amount=end, per=per,
    )


@pytest.fixture()
def catalog(tmp_path):
    return PricingCatalog(str(tmp_path / "pricing.db"))


class TestPerScalesBoundaries:
    """The multiplier converts a stated boundary into raw units."""

    def test_included_band_grows_with_the_parameter(self):
        """1,900 credits per seat, 25 seats, is 47,500 included."""
        tiers = TieredPrice(tiers=[
            row(0.0, start=0, end=1900, per="seats", metric="credits", unit="credits"),
            row(0.01, start=1900, per="seats", metric="credits", unit="credits"),
        ])
        assert tiers.total_cost(60000, per_multiplier=25) == pytest.approx(125.0)

    def test_under_the_scaled_boundary_is_free(self):
        tiers = TieredPrice(tiers=[
            row(0.0, start=0, end=1900, per="seats", metric="credits", unit="credits"),
            row(0.01, start=1900, per="seats", metric="credits", unit="credits"),
        ])
        assert tiers.total_cost(47500, per_multiplier=25) == pytest.approx(0.0)

    def test_a_boundary_without_per_does_not_move(self):
        tiers = TieredPrice(tiers=[
            row(0.0, start=0, end=1000000, metric="M2M-Tokens", unit="tokens"),
            row(0.000001, start=1000000, metric="M2M-Tokens", unit="tokens"),
        ])
        assert tiers.total_cost(3_000_000, per_multiplier=25) == pytest.approx(2.0)


class TestPerDoesNotScaleThePrice:
    """The reading that breaks the shipped Copilot seat row."""

    def test_a_flat_row_charges_price_times_quantity(self):
        """$19 per seat, 25 seats, is $475. Not $19."""
        tiers = TieredPrice(tiers=[row(19.0, per="seats", metric="seats", unit="seats")])
        assert tiers.total_cost(25, per_multiplier=25) == pytest.approx(475.0)

    def test_a_flat_row_without_per_is_the_same(self):
        tiers = TieredPrice(tiers=[row(19.0, metric="seats", unit="seats")])
        assert tiers.total_cost(25) == pytest.approx(475.0)

    def test_cost_result_agrees_with_tiered_price(self):
        result = _CostResult(
            TieredPrice(tiers=[row(19.0, per="seats", metric="seats", unit="seats")]),
            25, parameters={"seats": 25},
        )
        assert result.total_cost == pytest.approx(475.0)


class TestPerMustNotBeTheQuantitysOwnUnit:
    """A parameter equal to the quantity multiplies the boundary past it."""

    def test_clerk_m2m_prices_above_the_free_band(self, catalog):
        """1M tokens free, then $0.000001. 3M costs $2, not $0.

        The row carried `per: tokens`, so a 3,000,000-token query looked up a
        parameter of 3,000,000 and scaled the 1,000,000 boundary to 3e12. The
        whole bill fell inside the free band.
        """
        result = catalog.query("clerk", "Clerk", "global", "M2M-Tokens", 3_000_000)
        assert result is not None
        assert result.total_cost == pytest.approx(2.0)

    def test_no_bounded_row_declares_per_on_its_own_unit(self):
        """Guard the data rule, since the schema cannot state it.

        The rule applies to a row with boundaries. A boundary multiplied by a
        parameter of the same unit as the quantity moves past the quantity and
        the row prices at $0. A flat row has no boundary to move, so its
        multiplier is inert and `per` there is harmless documentation.
        """
        import re
        import pathlib

        offenders = []
        for path in sorted(pathlib.Path("vendors").glob("*/prices.yaml")):
            for block in re.split(r"\n(?=- vendor:)", path.read_text()):
                m = re.search(r"per:\s*(\S+)", block)
                if not m or m.group(1).startswith("<"):
                    continue
                if "start_usage_amount" not in block:
                    continue  # flat row: no boundary for the multiplier to move
                unit = re.search(r"unit:\s*(\S+)", block)
                if unit and unit.group(1).rstrip("s").lower() in m.group(1).lower():
                    offenders.append(f"{path.parent.name}: per={m.group(1)} unit={unit.group(1)}")
        assert not offenders, (
            "a bounded row's `per` parameter is its own quantity's unit, so "
            f"the multiplier cancels the pricing: {offenders}"
        )


class TestCopilotSeatPricingStillHolds:
    """The rows that worked keep working."""

    def test_seats_and_credits(self, catalog):
        seats = catalog.query("github", "Copilot", "global",
                              "Copilot-Seat-Month", 25, parameters={"seats": 25})
        credits = catalog.query("github", "Copilot", "global",
                                "Copilot-Credit", 60000, parameters={"seats": 25})
        assert seats.total_cost == pytest.approx(475.0)
        assert credits.total_cost == pytest.approx(125.0)
