import pytest
import tempfile
import os
from infra_cost_model.pricing.catalog import PricingCatalog

def test_copilot_per_evaluation():
    """Verify GitHub Copilot pricing scales with 'per: seats' parameter.

    Scenario:
    - Seats: $19/seat (flat)
    - Credits: First 1900 * seats are free, then $0.01/credit
    """
    db = tempfile.mktemp(suffix=".db")
    try:
        # PricingCatalog loads vendors automatically now
        catalog = PricingCatalog(db)

        # Test Case 1: 1 seat, 0 credits
        # Seat cost: 1 * 19 = 19
        # Credit cost: 0
        # Total: 19
        cost_seat_1 = catalog.query("github", "Copilot", "global", "Copilot-Seat-Month", 1, parameters={"seats": 1}).total_cost
        cost_credit_1 = catalog.query("github", "Copilot", "global", "Copilot-Credit", 0, parameters={"seats": 1}).total_cost
        assert cost_seat_1 == 19.0
        assert cost_credit_1 == 0.0
        assert cost_seat_1 + cost_credit_1 == 19.0

        # Test Case 2: 25 seats, 60k credits
        # Seat cost: 25 * 19 = 475
        # Credit cost: 60,000 - (1900 * 25) = 60,000 - 47,500 = 12,500 credits
        # 12,500 * 0.01 = 125
        # Total: 475 + 125 = 600
        cost_seat_25 = catalog.query("github", "Copilot", "global", "Copilot-Seat-Month", 25, parameters={"seats": 25}).total_cost
        cost_credit_25 = catalog.query("github", "Copilot", "global", "Copilot-Credit", 60000, parameters={"seats": 25}).total_cost
        assert cost_seat_25 == 475.0
        assert cost_credit_25 == 125.0
        assert cost_seat_25 + cost_credit_25 == 600.0

        # Test Case 3: 100 seats, 60k credits
        # Seat cost: 100 * 19 = 1900
        # Credit cost: 60,000 - (1900 * 100) = 60,000 - 190,000 = -130,000 -> 0
        # Total: 1900 + 0 = 1900
        cost_seat_100 = catalog.query("github", "Copilot", "global", "Copilot-Seat-Month", 100, parameters={"seats": 100}).total_cost
        cost_credit_100 = catalog.query("github", "Copilot", "global", "Copilot-Credit", 60000, parameters={"seats": 100}).total_cost
        assert cost_seat_100 == 1900.0
        assert cost_credit_100 == 0.0
        assert cost_seat_100 + cost_credit_100 == 1900.0

    finally:
        if os.path.exists(db):
            os.unlink(db)
