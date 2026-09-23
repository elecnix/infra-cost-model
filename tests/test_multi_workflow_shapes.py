"""SaaS shapes in models with several workflows (#305).

A shape's parameters, such as a free allowance or a subscription rate,
describe a month of the node's whole use. The engine used to call the shape
handler once per workflow, so a node reached by 2 workflows got its free
allowance twice and a usage-driven subscription billed twice. The engine now
adds up a month of use across the workflows and calls the handler once.
"""

import copy
from pathlib import Path

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.engine.engine import SECONDS_PER_MONTH
from infra_cost_model.sdk import parse_yaml_dsl

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def per_second(users_a_month):
    return {"unit": "perSecond", "value": users_a_month / SECONDS_PER_MONTH}


def model(metric, *monthly_counts):
    """1 node ``n`` reached by one workflow per entry in ``monthly_counts``."""
    return {
        "version": "1.0",
        "workflows": [
            {"name": f"w{i}", "entry": "n", "frequency": per_second(count)}
            for i, count in enumerate(monthly_counts)
        ],
        "nodes": {"n": {"nodeType": "external", "resourceAddress": "n",
                        "usageMetrics": {"users": metric}}},
        "edges": [],
    }


FREE_TIER = {"unit": "users", "value": 1, "shape": "free_tier",
             "free": 10_000, "overage": 0.02}
SUBSCRIPTION = {"unit": "calls", "value": 1, "shape": "flat_subscription",
                "rate": 49.0}


def monthly_total(m):
    return sum(CostEngine(m, catalog=None, time_basis="monthly").compute().values())


class TestFreeTier:
    def test_one_workflow(self):
        assert monthly_total(model(FREE_TIER, 20_000)) == pytest.approx(200)

    def test_the_allowance_applies_once_across_two_workflows(self):
        """10,000 + 10,000 users against 10,000 free: 10,000 × $0.02."""
        assert monthly_total(model(FREE_TIER, 10_000, 10_000)) == pytest.approx(200)

    def test_uneven_workflows(self):
        """15,000 + 5,000 users: the second workflow alone was under the
        allowance, the total is 10,000 over."""
        assert monthly_total(model(FREE_TIER, 15_000, 5_000)) == pytest.approx(200)

    @pytest.mark.parametrize("basis", ["perSecond", "yearly"])
    def test_other_bases_scale_the_same_monthly_cost(self, basis):
        factor = {"perSecond": 1 / SECONDS_PER_MONTH, "yearly": 12}[basis]
        m = model(FREE_TIER, 10_000, 10_000)
        got = sum(CostEngine(m, catalog=None, time_basis=basis).compute().values())
        assert got == pytest.approx(200 * factor)


class TestUsageDrivenSubscription:
    def test_bills_once_across_two_workflows(self):
        assert monthly_total(model(SUBSCRIPTION, 100, 100)) == pytest.approx(49)

    def test_bills_once_when_one_workflow_has_no_use(self):
        assert monthly_total(model(SUBSCRIPTION, 100, 0)) == pytest.approx(49)


def test_fixed_shape_keeps_the_last_workflow_rule():
    """A fixed metric is a property of the node, so it counts once."""
    fixed = {"unit": "seats", "value": 3, "fixed": True,
             "shape": "per_unit_flat", "rate": 10.0}
    assert monthly_total(model(fixed, 100, 100)) == pytest.approx(30)


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_data_pipeline_example_prices_a_shared_shape_once():
    """examples/data-pipeline.yaml has 2 workflows. An edge from the first
    workflow's transformer to the reports bucket, which the second workflow
    also writes to, makes the bucket shared. A free-tier metric on that bucket
    then gets one allowance for the use of both workflows."""
    base = parse_yaml_dsl((EXAMPLES_DIR / "data-pipeline.yaml").read_text())
    assert len(base["workflows"]) == 2
    base["edges"].append({"from": "aws_lambda_function.transformer",
                          "to": "aws_s3_bucket.reports", "rate": 1})
    shaped = copy.deepcopy(base)
    shaped["nodes"]["aws_s3_bucket.reports"]["usageMetrics"]["writes"] = {
        "unit": "writes", "value": 1, "shape": "free_tier",
        "free": 30_000, "overage": 0.01,
    }

    # 1,000 uploads a day plus 1 report a day, over a month.
    days = SECONDS_PER_MONTH / 86_400
    writes = 1_000 * days + 1 * days
    expected_shape_cost = (writes - 30_000) * 0.01
    assert expected_shape_cost > 0
    # Priced per workflow, the second workflow's 1 write a day was free.
    per_workflow = (1_000 * days - 30_000) * 0.01
    assert expected_shape_cost > per_workflow

    assert monthly_total(shaped) - monthly_total(base) == pytest.approx(
        expected_shape_cost)
