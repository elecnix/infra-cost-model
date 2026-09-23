"""Fixed costs at the per-second time basis (#304).

A fixed metric is a monthly total. At the per-second basis the engine used to
add that monthly total to per-second usage-driven costs, so one total mixed
two units. Each basis now converts both parts to the same unit: per second
divides the fixed part by the seconds in a month, monthly keeps it, and yearly
multiplies it by 12.
"""

import io
import re
import sys
import warnings

import pytest

from infra_cost_model.cli import main
from infra_cost_model.engine import CostEngine
from infra_cost_model.engine.engine import SECONDS_PER_MONTH, SensitivityAnalyzer

# 1 request per second, so a month holds SECONDS_PER_MONTH requests.
FREQUENCY = {"unit": "perSecond", "value": 1}
REQUEST_RATE = 0.20e-6
SUBSCRIPTION = 49.0


def fixed_node():
    return {
        "nodeType": "external",
        "resourceAddress": "n",
        "usageMetrics": {
            "plan": {"unit": "months", "value": 1, "fixed": True,
                     "shape": "flat_subscription", "rate": SUBSCRIPTION},
        },
    }


def usage_node():
    return {
        "nodeType": "compute",
        "resourceAddress": "n",
        "usageMetrics": {"requests": {"unit": "requests", "value": 1}},
        "pricingRates": {"requests": REQUEST_RATE},
    }


def mixed_node():
    node = usage_node()
    node["usageMetrics"]["plan"] = fixed_node()["usageMetrics"]["plan"]
    return node


def single(node):
    return {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "n", "frequency": dict(FREQUENCY)},
        "nodes": {"n": node},
        "edges": [],
    }


def multi(node):
    return {
        "version": "1.0",
        "workflows": [{"name": "w", "entry": "n", "frequency": dict(FREQUENCY)}],
        "nodes": {"n": node},
        "edges": [],
    }


MONTHLY = {
    "fixed": SUBSCRIPTION,
    "usage": REQUEST_RATE * SECONDS_PER_MONTH,
    "mixed": SUBSCRIPTION + REQUEST_RATE * SECONDS_PER_MONTH,
}
NODES = {"fixed": fixed_node, "usage": usage_node, "mixed": mixed_node}
PER_BASIS = {"perSecond": 1 / SECONDS_PER_MONTH, "monthly": 1.0, "yearly": 12.0}


def total(model, basis):
    return sum(CostEngine(model, catalog=None, time_basis=basis).compute().values())


@pytest.mark.parametrize("build", [single, multi], ids=["workflow", "workflows"])
@pytest.mark.parametrize("basis", list(PER_BASIS))
@pytest.mark.parametrize("kind", list(NODES))
def test_every_basis_is_the_monthly_total_in_its_own_unit(kind, basis, build):
    expected = MONTHLY[kind] * PER_BASIS[basis]
    assert total(build(NODES[kind]()), basis) == pytest.approx(expected)


def test_issue_example_fixed_subscription_per_second():
    """$49 a month is about $0.0000186 a second."""
    assert total(single(fixed_node()), "perSecond") == pytest.approx(
        49 / SECONDS_PER_MONTH)
    assert total(single(fixed_node()), "perSecond") == pytest.approx(
        0.0000186, rel=1e-2)


def test_unpriced_fixed_quantity_is_per_second():
    node = usage_node()
    node["usageMetrics"]["seats"] = {"unit": "seats", "value": 25, "fixed": True}
    engine = CostEngine(single(node), catalog=None, time_basis="perSecond")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        engine.compute()
    by_metric = {u.metric: u for u in engine.unpriced_metrics}
    assert by_metric["seats"].quantity == pytest.approx(25 / SECONDS_PER_MONTH)


def test_what_if_compares_totals_in_one_unit():
    """Doubling the traffic doubles the usage part and keeps the fixed part."""
    analyzer = SensitivityAnalyzer(single(mixed_node()), None,
                                   time_basis="perSecond")
    doubled = analyzer.what_if("frequency", 2)
    expected = (SUBSCRIPTION + 2 * REQUEST_RATE * SECONDS_PER_MONTH) / SECONDS_PER_MONTH
    assert doubled == pytest.approx(expected)


def test_cli_labels_the_per_second_total(tmp_path):
    import yaml
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(single(mixed_node())))
    # No catalog: the node prices from its shape and pricingRates.
    old = sys.stdout
    sys.stdout = io.StringIO()
    try:
        assert main(["compute", "--no-catalog", str(path)]) == 0
        out = sys.stdout.getvalue()
    finally:
        sys.stdout = old
    match = re.search(r"^Total Per-Second Cost: \$([\d.]+)$", out, re.MULTILINE)
    assert match, out
    assert float(match.group(1)) == pytest.approx(
        MONTHLY["mixed"] / SECONDS_PER_MONTH, abs=1e-6)


@pytest.mark.parametrize("argv", [
    ["whatif", "--parameter", "frequency", "--value", "2"],
    ["sensitivity", "--parameter", "frequency", "--steps", "3"],
], ids=lambda argv: argv[0])
def test_what_if_and_sensitivity_name_the_per_second_basis(argv, tmp_path, capsys):
    import yaml
    path = tmp_path / "m.yaml"
    path.write_text(yaml.safe_dump(single(mixed_node())))
    assert main([argv[0], str(path), *argv[1:]]) == 0
    assert "(per second)" in capsys.readouterr().out.splitlines()[0]
