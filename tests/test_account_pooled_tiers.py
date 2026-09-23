"""Catalog tiers apply once per account, not once per node (Issue #294).

AWS gives the SQS free tier (1,000,000 requests a month) to the account,
across every queue in the region. The engine adds up the monthly quantity
for each (provider, service, region, usage metric) across all nodes and
workflows, prices the total once, and splits the cost across the nodes in
proportion to their quantities.
"""

import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.resources.lambda_func import LambdaFunction


def queue(value=1, fixed=False):
    metric = {"unit": "requests", "value": value}
    if fixed:
        metric["fixed"] = True
    return {
        "nodeType": "queue",
        "provider": "aws",
        "service": "AmazonSQS",
        "region": "us-east-1",
        "usageMetrics": {"SQS-Standard-Request": metric},
    }


def per_month(value):
    return {"unit": "perMonth", "value": value}


def compute(model, time_basis="monthly"):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        engine = CostEngine(model, catalog=PricingCatalog(seed=True),
                            time_basis=time_basis)
        return engine.compute()


def two_queues(requests_a, requests_b):
    return {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "a", "frequency": per_month(requests_a)},
        "nodes": {"a": queue(), "b": queue()},
        "edges": [{"from": "a", "to": "b", "type": "async",
                   "rate": requests_b / requests_a}],
    }


def test_two_queues_share_one_free_tier():
    # 2,000,000 requests, the first 1,000,000 free, then $0.0000004 each.
    costs = compute(two_queues(1_000_000, 1_000_000))
    assert sum(costs.values()) == pytest.approx(0.40, rel=1e-9)
    assert costs["a"] == pytest.approx(0.20, rel=1e-9)
    assert costs["b"] == pytest.approx(0.20, rel=1e-9)


def test_one_queue_with_the_same_total_costs_the_same():
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "a", "frequency": per_month(2_000_000)},
        "nodes": {"a": queue()},
        "edges": [],
    }
    assert compute(model)["a"] == pytest.approx(0.40, rel=1e-9)


def test_cost_splits_in_proportion_to_quantity():
    # 1,000,000 + 3,000,000 requests: 3,000,000 paid, $1.20 in total.
    costs = compute(two_queues(1_000_000, 3_000_000))
    assert costs["a"] == pytest.approx(1.20 * 1 / 4, rel=1e-9)
    assert costs["b"] == pytest.approx(1.20 * 3 / 4, rel=1e-9)


def test_pooled_total_is_the_same_in_every_time_basis():
    model = two_queues(1_000_000, 1_000_000)
    monthly = sum(compute(model, "monthly").values())
    yearly = sum(compute(model, "yearly").values())
    per_second = sum(compute(model, "perSecond").values())
    assert yearly == pytest.approx(12 * monthly, rel=1e-9)
    assert per_second == pytest.approx(monthly / (86400 * 365.25 / 12), rel=1e-9)


def shared_queue_in_two_workflows():
    return {
        "version": "1.0",
        "workflows": [
            {"name": "w1", "entry": "q", "frequency": per_month(1_000_000)},
            {"name": "w2", "entry": "q", "frequency": per_month(1_000_000)},
        ],
        "nodes": {"q": queue()},
        "edges": [],
    }


def test_a_node_shared_by_two_workflows_gets_the_free_tier_once():
    costs = compute(shared_queue_in_two_workflows())
    assert costs["q"] == pytest.approx(0.40, rel=1e-9)


def test_shared_node_yearly_total_is_twelve_months():
    costs = compute(shared_queue_in_two_workflows(), "yearly")
    assert costs["q"] == pytest.approx(12 * 0.40, rel=1e-9)


def test_fixed_quantities_pool_once_across_workflows():
    # Two queues with a fixed 1,000,000 requests a month each. Both
    # workflows reach both queues, but a fixed quantity counts once.
    model = {
        "version": "1.0",
        "workflows": [
            {"name": "w1", "entry": "a", "frequency": per_month(1)},
            {"name": "w2", "entry": "b", "frequency": per_month(1)},
        ],
        "nodes": {"a": queue(1_000_000, fixed=True),
                  "b": queue(1_000_000, fixed=True)},
        "edges": [],
    }
    monthly = compute(model)
    assert monthly["a"] == pytest.approx(0.20, rel=1e-9)
    assert monthly["b"] == pytest.approx(0.20, rel=1e-9)
    yearly = compute(model, "yearly")
    assert sum(yearly.values()) == pytest.approx(12 * 0.40, rel=1e-9)


def test_fixed_and_usage_driven_quantities_pool_together():
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "a", "frequency": per_month(1_000_000)},
        "nodes": {"a": queue(), "b": queue(1_000_000, fixed=True)},
        "edges": [],
    }
    costs = compute(model)
    assert costs["a"] == pytest.approx(0.20, rel=1e-9)
    assert costs["b"] == pytest.approx(0.20, rel=1e-9)


def lambda_node(name):
    extract = LambdaFunction.extract_tf({
        "address": f"aws_lambda_function.{name}",
        "type": "aws_lambda_function",
        "values": {"memory_size": 1024, "timeout": 30, "runtime": "python3.12",
                   "region": "us-east-1"},
    })
    return {
        "nodeType": extract.node_type,
        "resourceAddress": extract.resource_address,
        "provider": extract.provider,
        "service": extract.service,
        "region": extract.region,
        "usageMetrics": {
            "invocations": {"unit": "requests", "value": 1},
            "avgDurationMs": {"unit": "ms", "value": 1000},
            "memoryMb": {"unit": "MB", "value": extract.config["memoryMb"]},
        },
    }


def test_lambda_free_tier_is_shared_by_two_functions():
    # Each function: 1,000,000 requests and 1,000,000 GB-seconds a month.
    # Together: 1,000,000 paid requests at $0.0000002 and 1,600,000 paid
    # GB-seconds (above 400,000 free) at $0.0000166667.
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "a", "frequency": per_month(1_000_000)},
        "nodes": {"a": lambda_node("a"), "b": lambda_node("b")},
        "edges": [{"from": "a", "to": "b", "type": "sync", "rate": 1}],
    }
    costs = compute(model)
    total = 1_000_000 * 0.0000002 + 1_600_000 * 0.0000166667
    assert sum(costs.values()) == pytest.approx(total, rel=1e-9)
    assert costs["a"] == pytest.approx(total / 2, rel=1e-9)
    assert costs["b"] == pytest.approx(total / 2, rel=1e-9)


def copilot_node(metrics):
    return {
        "nodeType": "external",
        "provider": "github",
        "service": "Copilot",
        "region": "global",
        "flatOverride": True,
        "usageMetrics": {name: {"unit": "units", "value": value}
                         for name, value in metrics.items()},
    }


def test_copilot_credits_pool_against_the_per_seat_allowance():
    # 25 seats give 47,500 included credits. Two teams use 30,000 credits
    # each: 12,500 over at $0.01, split evenly between the two nodes.
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "a", "frequency": per_month(1),
                     "parameters": {"seats": 25}},
        "nodes": {
            "a": copilot_node({"Copilot-Seat-Month": 25, "Copilot-Credit": 30_000}),
            "b": copilot_node({"Copilot-Credit": 30_000}),
        },
        "edges": [],
    }
    costs = compute(model)
    assert costs["a"] == pytest.approx(25 * 19.0 + 62.50, rel=1e-9)
    assert costs["b"] == pytest.approx(62.50, rel=1e-9)


def test_flat_rows_do_not_change():
    # NAT gateway hours have one flat price and no tiers.
    def nat(hours):
        return {"nodeType": "network", "provider": "aws", "service": "AmazonVPC",
                "region": "us-east-1",
                "usageMetrics": {"NAT-Gateway-Hour": {"unit": "Hours",
                                                      "value": hours,
                                                      "fixed": True}}}
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "a", "frequency": per_month(1)},
        "nodes": {"a": nat(730), "b": nat(100)},
        "edges": [],
    }
    costs = compute(model)
    assert costs["a"] == pytest.approx(730 * 0.045, rel=1e-9)
    assert costs["b"] == pytest.approx(100 * 0.045, rel=1e-9)
