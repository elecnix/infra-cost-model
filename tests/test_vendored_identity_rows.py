"""Every priced band the identity-provider module carried is still priced.

The Python module `infra_cost_model/pricing/identity_providers.py` held a
price band per vendor, and the port to `vendors/<id>/prices.yaml` had to carry
each one across. Two bands were dropped: Frontegg's MAU overage above 10,000
and Kinde's above 10,500. Both vendors kept their free band and gained an SSO
row in the same hunk, so the files looked populated while the overage priced
at $0.

These tests price a quantity inside each overage band, which is the assertion
that fails when the row is missing. A test that only checked the row exists
would pass on a file that declared the band with the wrong rate.
"""

import pytest

from infra_cost_model.pricing.catalog import PricingCatalog


@pytest.fixture()
def catalog(tmp_path):
    return PricingCatalog(str(tmp_path / "pricing.db"))


def price(catalog, vendor, service, metric, quantity):
    result = catalog.query(vendor, service, "global", metric, quantity)
    assert result is not None, f"{vendor}/{metric} has no rows"
    return result.total_cost


class TestMauOverageIsPriced:
    """A vendor with a free band also has a paid band above it."""

    def test_frontegg_overage_above_ten_thousand(self, catalog):
        """Frontegg: 10,000 free, then $0.03/MAU. 15,000 costs $150."""
        assert price(catalog, "frontegg", "Frontegg", "MAU", 15000) == pytest.approx(150.0)

    def test_kinde_overage_above_ten_thousand_five_hundred(self, catalog):
        """Kinde: 10,500 free, then $0.01/MAU. 15,000 costs $45."""
        assert price(catalog, "kinde", "Kinde", "MAU", 15000) == pytest.approx(45.0)

    def test_frontegg_inside_the_free_band_is_free(self, catalog):
        """The free band stays free, so the fix does not overcharge."""
        assert price(catalog, "frontegg", "Frontegg", "MAU", 10000) == pytest.approx(0.0)

    def test_kinde_inside_the_free_band_is_free(self, catalog):
        assert price(catalog, "kinde", "Kinde", "MAU", 10500) == pytest.approx(0.0)


class TestTheOtherPortedVendorsPriceTheirOverage:
    """The same band is present for the vendors that kept it."""

    def test_auth0_overage_above_twenty_five_thousand(self, catalog):
        """Auth0: 25,000 free, then $0.07/MAU. 30,000 costs $350."""
        assert price(catalog, "auth0", "Auth0", "MAU", 30000) == pytest.approx(350.0)

    def test_clerk_overage_above_ten_thousand(self, catalog):
        """Clerk steps: 10,000 free, then $0.02/MAU. 20,000 costs $200."""
        assert price(catalog, "clerk", "Clerk", "MAU", 20000) == pytest.approx(200.0)

    def test_cognito_overage_above_fifty_thousand(self, catalog):
        """Cognito: 50,000 free, then $0.0055/MAU. 60,000 costs $55."""
        assert price(catalog, "aws", "Cognito", "MAU", 60000) == pytest.approx(55.0)
