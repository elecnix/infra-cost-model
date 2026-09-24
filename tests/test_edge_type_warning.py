"""A metric whose edge type never reaches its node is reported (#322).

A usage metric with `edgeType` counts only the calls that arrive over edges of
that type (#313). When the node receives calls, but none over that type, the
metric counts zero calls and prices $0. That usually means an edge lacks its
`type`, so the engine warns, `compute` prints the warning, and `validate`
reports the same case with the same rule.
"""

import warnings

import pytest
import yaml

from infra_cost_model.cli import main
from infra_cost_model.engine import CostEngine, EdgeTypeMetricWarning
from infra_cost_model.engine.engine import edge_type_metric_warnings

READ_METRIC = {"unit": "requests", "value": 1, "edgeType": "read"}


def model(metrics=None, edge_type=None):
    """An API that calls a table over one edge, untyped unless given a type."""
    edge = {"from": "api", "to": "table", "rate": 1}
    if edge_type:
        edge["type"] = edge_type
    return {
        "version": "1.0",
        "workflow": {
            "name": "items",
            "entry": "api",
            "frequency": {"unit": "perSecond", "value": 1},
        },
        "nodes": {
            "api": {"nodeType": "routing", "resourceAddress": "api"},
            "table": {
                "nodeType": "storage",
                "resourceAddress": "table",
                "provider": "external",
                "region": "global",
                "usageMetrics": metrics or {"Dynamo-ReadRequest": READ_METRIC},
                "pricingRates": {"Dynamo-ReadRequest": 1.25e-6},
            },
        },
        "edges": [edge],
    }


def compute(cost_model):
    engine = CostEngine(cost_model)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine.compute()
    found = [str(w.message) for w in caught
             if issubclass(w.category, EdgeTypeMetricWarning)]
    return engine, found


class TestEngine:
    def test_warns_when_no_edge_of_the_metric_type_reaches_the_node(self):
        engine, found = compute(model())
        assert len(found) == 1
        message = found[0]
        assert "'table'" in message
        assert "'Dynamo-ReadRequest'" in message
        assert "read" in message
        assert "invoke" in message  # the edge type the table does receive
        assert engine.edge_type_warnings == found

    def test_no_warning_when_an_edge_of_the_metric_type_reaches_the_node(self):
        engine, found = compute(model(edge_type="read"))
        assert found == []
        assert engine.edge_type_warnings == []

    def test_no_warning_for_a_metric_without_edge_type(self):
        _, found = compute(model(metrics={"Dynamo-ReadRequest": {"value": 1}}))
        assert found == []

    def test_no_warning_for_a_fixed_metric(self):
        metrics = {"Dynamo-ReadRequest": {**READ_METRIC, "fixed": True}}
        _, found = compute(model(metrics=metrics))
        assert found == []

    def test_no_warning_for_a_node_no_edge_reaches(self):
        # The unreachable-node warning already covers this node.
        cost_model = model()
        cost_model["edges"] = []
        _, found = compute(cost_model)
        assert found == []

    def test_workflows_that_reach_the_node_over_other_types_count_together(self):
        cost_model = model(edge_type="write")
        cost_model["nodes"]["reader"] = {"nodeType": "compute", "resourceAddress": "reader"}
        cost_model["edges"].append(
            {"from": "reader", "to": "table", "rate": 1, "type": "read"})
        workflow = cost_model.pop("workflow")
        cost_model["workflows"] = [workflow, {**workflow, "name": "reads", "entry": "reader"}]
        _, found = compute(cost_model)
        assert found == []


class TestSharedRule:
    def test_validate_and_the_engine_give_the_same_message(self):
        _, found = compute(model())
        assert edge_type_metric_warnings(model()) == found

    def test_the_rule_counts_entry_traffic_as_invoke(self):
        cost_model = model(metrics={"x": {"value": 1, "edgeType": "read"}})
        cost_model["nodes"]["api"]["usageMetrics"] = {"x": {"value": 1, "edgeType": "read"}}
        messages = edge_type_metric_warnings(cost_model)
        assert any("'api'" in m for m in messages)


class TestCli:
    def write(self, tmp_path, cost_model):
        path = tmp_path / "model.yaml"
        path.write_text(yaml.safe_dump(cost_model))
        return str(path)

    def test_compute_prints_the_warning_once(self, tmp_path, capsys):
        path = self.write(tmp_path, model())
        assert main(["compute", "--no-catalog", path]) == 0
        err = capsys.readouterr().err
        assert err.count("'Dynamo-ReadRequest'") == 1
        assert "Warning:" in err

    def test_validate_reports_the_warning_and_passes(self, tmp_path, capsys):
        path = self.write(tmp_path, model())
        assert main(["validate", path]) == 0
        captured = capsys.readouterr()
        assert "'Dynamo-ReadRequest'" in captured.err
        assert "Warning:" in captured.err

    def test_validate_is_silent_on_a_typed_edge(self, tmp_path, capsys):
        path = self.write(tmp_path, model(edge_type="read"))
        assert main(["validate", path]) == 0
        assert capsys.readouterr().err == ""


@pytest.mark.parametrize("edge_type", ["read", "write", "invoke"])
def test_each_edge_type_is_checked(edge_type):
    others = {"read": "write", "write": "invoke", "invoke": "read"}
    metrics = {"m": {"value": 1, "edgeType": edge_type}}
    _, found = compute(model(metrics=metrics, edge_type=others[edge_type]))
    assert len(found) == 1
