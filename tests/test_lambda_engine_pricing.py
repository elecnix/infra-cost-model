"""The engine prices extracted Lambda nodes from the catalog (Issue #276)."""

import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.engine.engine import UnpricedMetricWarning
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.resources.lambda_func import LambdaFunction

TF_RESOURCE = {
    "address": "aws_lambda_function.fn",
    "type": "aws_lambda_function",
    "values": {"memory_size": 1024, "timeout": 30, "runtime": "python3.12",
               "region": "us-east-1"},
}


def lambda_model(memory_mb=1024, duration_ms=1000, region="us-east-1",
                 pricing_model=None, invocations_per_month=1_000_000):
    extract = LambdaFunction.extract_tf(dict(TF_RESOURCE, values=dict(
        TF_RESOURCE["values"], memory_size=memory_mb, region=region)))
    node = {
        "nodeType": extract.node_type,
        "resourceAddress": extract.resource_address,
        "provider": extract.provider,
        "service": extract.service,
        "region": extract.region,
        "usageMetrics": {
            "invocations": {"unit": "requests", "value": 1},
            "avgDurationMs": {"unit": "ms", "value": duration_ms},
            "memoryMb": {"unit": "MB", "value": extract.config["memoryMb"]},
        },
    }
    if pricing_model:
        node["pricingModel"] = pricing_model
    return {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "fn",
                     "frequency": {"unit": "perMonth", "value": invocations_per_month}},
        "nodes": {"fn": node},
        "edges": [],
    }


def compute_monthly(model):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine = CostEngine(model, catalog=PricingCatalog(seed=True),
                            time_basis="monthly")
        costs = engine.compute()
    unpriced = [w.message.unpriced.metric for w in caught
                if isinstance(w.message, UnpricedMetricWarning)]
    return costs["fn"], unpriced


@pytest.mark.parametrize("pricing_model", [None, "flat", "tiered"])
def test_extracted_lambda_prices_requests_and_gb_seconds(pricing_model):
    cost, unpriced = compute_monthly(lambda_model(
        pricing_model=pricing_model, invocations_per_month=3_000_000))
    # 2M requests above the 1M free requests at $0.20 per million, plus
    # 2.6M GB-seconds above the 400,000 free GB-seconds at $0.0000166667
    # (#287: the monthly free tier applies).
    assert cost == pytest.approx(0.40 + 2_600_000 * 0.0000166667, rel=1e-6)
    assert unpriced == []


def test_gb_seconds_scale_with_memory_and_duration():
    cost, _ = compute_monthly(lambda_model(memory_mb=512, duration_ms=200,
                                           invocations_per_month=10_000_000))
    # 10M × 0.5 GB × 0.2 s = 1,000,000 GB-seconds, 600,000 of them above
    # the free tier. 9M requests above the free tier cost $1.80.
    assert cost == pytest.approx(1.80 + 600_000 * 0.0000166667, rel=1e-6)


def test_region_without_lambda_rows_still_warns_about_logical_metrics():
    cost, unpriced = compute_monthly(lambda_model(region="ap-south-2"))
    assert cost == 0
    assert sorted(unpriced) == ["avgDurationMs", "invocations", "memoryMb"]


def test_catalog_query_can_leave_out_the_free_tier():
    catalog = PricingCatalog(seed=True)
    with_free = catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-Request", 1_500_000)
    without_free = catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-Request",
                                 1_500_000, include_free_tier=False)
    assert with_free.total_cost == pytest.approx(0.10)
    assert without_free.total_cost == pytest.approx(0.30)
