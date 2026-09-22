"""Tests for the yearly time basis, in both workflow paths.

`--time-basis yearly` scales usage-driven cost by the seconds in a month times
twelve. Always-on cost is a flat monthly total, so it scales by twelve alone.
The single-workflow path applied that scaling to the fixed part from the start;
the multi-workflow path did not, so a model using the `workflows` array
reported yearly fixed costs at monthly scale.

These tests pin the two paths to the same answer for the same input, which is
the property that was missing.
"""

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.engine.engine import SECONDS_PER_MONTH


def fixed_only_node():
    """A node whose whole cost is a flat monthly total."""
    return {
        "nodeType": "compute",
        "resourceAddress": "n",
        "provider": "aws",
        "service": "AWSLambda",
        "region": "us-east-1",
        "usageMetrics": {"h": {"unit": "hours", "value": 730, "fixed": True}},
        "pricingRates": {"h": 0.1},
    }


def usage_node():
    """A node whose cost scales with the derived invocation count."""
    return {
        "nodeType": "compute",
        "resourceAddress": "n",
        "provider": "aws",
        "service": "AWSLambda",
        "region": "us-east-1",
        "usageMetrics": {"invocations": {"unit": "requests", "value": 1}},
        "pricingRates": {"invocations": 0.20e-6},
    }


def single_workflow_model(node):
    return {
        "version": "1.0",
        "workflow": {
            "name": "w",
            "entry": "n",
            "frequency": {"unit": "perMinute", "value": 60},
        },
        "nodes": {"n": node},
        "edges": [],
    }


def multi_workflow_model(node):
    return {
        "version": "1.0",
        "workflows": [
            {
                "name": "w",
                "entry": "n",
                "frequency": {"unit": "perMinute", "value": 60},
            }
        ],
        "nodes": {"n": node},
        "edges": [],
    }


class TestFixedCostsScaleForYearly:
    """A flat monthly total is twelve times larger over a year."""

    def test_single_workflow_scales_fixed_by_twelve(self):
        monthly = sum(
            CostEngine(single_workflow_model(fixed_only_node()),
                       catalog=None, time_basis="monthly").compute().values()
        )
        yearly = sum(
            CostEngine(single_workflow_model(fixed_only_node()),
                       catalog=None, time_basis="yearly").compute().values()
        )
        assert yearly == pytest.approx(monthly * 12)

    def test_multi_workflow_scales_fixed_by_twelve(self):
        monthly = sum(
            CostEngine(multi_workflow_model(fixed_only_node()),
                       catalog=None, time_basis="monthly").compute().values()
        )
        yearly = sum(
            CostEngine(multi_workflow_model(fixed_only_node()),
                       catalog=None, time_basis="yearly").compute().values()
        )
        assert yearly == pytest.approx(monthly * 12)


class TestTheTwoPathsAgree:
    """Same input, same basis, same total. The property that was missing."""

    @pytest.mark.parametrize("node", [fixed_only_node(), usage_node()])
    @pytest.mark.parametrize("basis", ["perSecond", "monthly", "yearly"])
    def test_single_and_multi_agree(self, node, basis):
        single = sum(
            CostEngine(single_workflow_model(node), catalog=None,
                       time_basis=basis).compute().values()
        )
        multi = sum(
            CostEngine(multi_workflow_model(node), catalog=None,
                       time_basis=basis).compute().values()
        )
        assert multi == pytest.approx(single)


class TestMixedFixedAndUsage:
    """A node with both dimensions scales each by its own factor."""

    def test_fixed_and_usage_scaled_separately(self):
        node = {
            "nodeType": "compute",
            "resourceAddress": "n",
            "provider": "aws",
            "service": "AWSLambda",
            "region": "us-east-1",
            "usageMetrics": {
                "h": {"unit": "hours", "value": 730, "fixed": True},
                "invocations": {"unit": "requests", "value": 1},
            },
            "pricingRates": {"h": 0.1, "invocations": 0.20e-6},
        }
        monthly = sum(
            CostEngine(single_workflow_model(node), catalog=None,
                       time_basis="monthly").compute().values()
        )
        yearly = sum(
            CostEngine(single_workflow_model(node), catalog=None,
                       time_basis="yearly").compute().values()
        )
        # Variable cost alone for a year is the monthly figure x 12.
        assert yearly == pytest.approx(monthly * 12)
