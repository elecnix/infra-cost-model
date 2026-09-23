"""Tier boundaries in catalog rows are monthly quantities (Issue #287).

The engine derives usage per second. The seed and vendor rows state their
tier boundaries, such as a free allowance, per month. So the engine prices
a month of usage against the boundaries and then converts the cost to the
output time basis.
"""

import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.engine.engine import SECONDS_PER_MONTH, UnpricedMetricWarning
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.vendors import load_vendor_prices


def one_node_model(service, region, metric, per_month, pricing_model=None,
                   parameters=None, provider="aws", fixed_metrics=None):
    node = {
        "nodeType": "compute",
        "provider": provider,
        "service": service,
        "region": region,
        "usageMetrics": {metric: {"unit": "units", "value": 1}},
    }
    for name, value in (fixed_metrics or {}).items():
        node["usageMetrics"][name] = {"unit": "units", "value": value, "fixed": True}
    if pricing_model:
        node["pricingModel"] = pricing_model
    workflow = {"name": "w", "entry": "n",
                "frequency": {"unit": "perMonth", "value": per_month}}
    if parameters:
        workflow["parameters"] = parameters
    return {"version": "1.0", "workflow": workflow, "nodes": {"n": node},
            "edges": []}


def compute(model, time_basis="monthly", catalog=None):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine = CostEngine(model, catalog=catalog or PricingCatalog(seed=True),
                            time_basis=time_basis)
        costs = engine.compute()
    unpriced = [w.message.unpriced.metric for w in caught
                if isinstance(w.message, UnpricedMetricWarning)]
    return costs["n"], unpriced


def sqs(per_month, pricing_model=None):
    return one_node_model("AmazonSQS", "us-east-1", "SQS-Standard-Request",
                          per_month, pricing_model)


@pytest.mark.parametrize("pricing_model", [None, "flat", "tiered"])
def test_quantity_below_the_free_allowance_costs_nothing(pricing_model):
    cost, unpriced = compute(sqs(500_000, pricing_model))
    assert cost == pytest.approx(0.0, abs=1e-9)
    assert unpriced == []


@pytest.mark.parametrize("pricing_model", [None, "flat", "tiered"])
def test_quantity_above_the_free_allowance_pays_for_the_excess(pricing_model):
    # 4,000,000 requests above the 1,000,000 free tier at $0.0000004.
    cost, unpriced = compute(sqs(5_000_000, pricing_model))
    assert cost == pytest.approx(1.60, rel=1e-9)
    assert unpriced == []


def test_quantity_crossing_a_paid_tier_boundary():
    # CloudFront: the first 1,024 GB free (#333), $0.085 per GB up to
    # 10,240 GB, then $0.080 up to 51,200 GB.
    model = one_node_model("AmazonCloudFront", "global",
                           "CloudFront-DataTransfer", 20_480, "tiered")
    cost, _ = compute(model)
    assert cost == pytest.approx(9_216 * 0.085 + 10_240 * 0.080, rel=1e-9)


@pytest.mark.parametrize("time_basis, months", [
    ("perSecond", 1 / SECONDS_PER_MONTH),
    ("monthly", 1.0),
    ("yearly", 12.0),
])
def test_every_time_basis_gives_the_same_monthly_equivalent(time_basis, months):
    cost, _ = compute(sqs(5_000_000), time_basis=time_basis)
    assert cost / months == pytest.approx(1.60, rel=1e-9)


def test_lambda_gets_its_monthly_free_tier():
    """1,000,000 invocations at 1,024 MB and 1 s: the requests fit in the
    1,000,000 free requests, and 600,000 of the 1,000,000 GB-seconds are
    above the 400,000 free GB-seconds."""
    model = one_node_model("AWSLambda", "us-east-1", "invocations", 1_000_000)
    node = model["nodes"]["n"]
    node["resourceAddress"] = "aws_lambda_function.fn"
    node["usageMetrics"].update({
        "avgDurationMs": {"unit": "ms", "value": 1000},
        "memoryMb": {"unit": "MB", "value": 1024},
    })
    cost, unpriced = compute(model)
    assert cost == pytest.approx(600_000 * 0.0000166667, rel=1e-6)
    assert unpriced == []


def vendor_catalog(tmp_path):
    catalog = PricingCatalog(db_path=tmp_path / "prices.db", seed=True)
    load_vendor_prices(catalog._cache)
    return catalog


def test_per_seat_boundaries_scale_a_usage_driven_quantity(tmp_path):
    """60,000 credits a month against 1,900 included credits per seat: at 25
    seats, 47,500 are included and 12,500 cost $0.01 each."""
    model = one_node_model("Copilot", "global", "Copilot-Credit", 60_000,
                           parameters={"seats": 25}, provider="github")
    cost, unpriced = compute(model, catalog=vendor_catalog(tmp_path))
    assert cost == pytest.approx(12_500 * 0.01, rel=1e-9)
    assert unpriced == []


def test_fixed_quantities_are_already_monthly(tmp_path):
    """A fixed metric states a monthly total, so its boundaries apply to the
    value as written, and yearly output is 12 months of it."""
    model = one_node_model("Copilot", "global", "Copilot-Credit", 0,
                           parameters={"seats": 25}, provider="github",
                           fixed_metrics={"Copilot-Credit": 60_000})
    catalog = vendor_catalog(tmp_path)
    monthly, _ = compute(model, catalog=catalog)
    yearly, _ = compute(model, time_basis="yearly", catalog=catalog)
    assert monthly == pytest.approx(125.0, rel=1e-9)
    assert yearly == pytest.approx(12 * 125.0, rel=1e-9)
