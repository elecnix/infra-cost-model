"""A metric the engine cannot price must not vanish from the total unannounced.

Before this change, a usage metric with no ``shape``, no catalog row and no
``pricingRates`` entry added $0 to its node, and the run exited 0 (the CloudFront
``region: global`` case in issue #266). The engine now records each such metric
on ``CostEngine.unpriced_metrics``, emits an ``UnpricedMetricWarning``, and the
CLI reports it on stderr.
"""

import copy
import json
import warnings

import pytest
import yaml

from infra_cost_model.cli import main
from infra_cost_model.engine import CostEngine, UnpricedMetric, UnpricedMetricWarning
from infra_cost_model.engine.engine import SECONDS_PER_MONTH
from infra_cost_model.pricing.catalog import PricingCatalog


def _model(node: dict, name: str = "unpriced") -> dict:
    return {
        "version": "1.0",
        "workflow": {
            "name": name,
            "entry": "svc.api",
            "frequency": {"unit": "perSecond", "value": 1},
        },
        "nodes": {"svc.api": node},
        "edges": [],
    }


FLAT_NODE = {
    "nodeType": "compute",
    "resourceAddress": "svc.api",
    "provider": "acme",
    "service": "Widgets",
    "region": "global",
    "pricingModel": "flat",
    "usageMetrics": {
        "requests": {"unit": "requests", "value": 2},
        "gbOut": {"unit": "GB", "value": 3},
    },
    "pricingRates": {"requests": 0.5},
}


def _compute(model, **kwargs):
    engine = CostEngine(model, **kwargs)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        costs = engine.compute()
    unpriced_warnings = [w for w in caught if issubclass(w.category, UnpricedMetricWarning)]
    return engine, costs, unpriced_warnings


class TestEngineRecordsUnpricedMetrics:
    def test_metric_without_any_price_is_recorded(self):
        engine, costs, caught = _compute(_model(copy.deepcopy(FLAT_NODE)))

        assert costs["svc.api"] == pytest.approx(1.0)
        assert engine.unpriced_metrics == [
            UnpricedMetric(
                node="svc.api", metric="gbOut", provider="acme",
                service="Widgets", region="global", quantity=3.0,
                time_basis="perSecond",
            )
        ]
        assert len(caught) == 1
        message = str(caught[0].message)
        assert "svc.api" in message and "gbOut" in message
        assert caught[0].message.unpriced == engine.unpriced_metrics[0]

    def test_priced_model_records_nothing(self):
        node = copy.deepcopy(FLAT_NODE)
        node["pricingRates"]["gbOut"] = 0.1
        engine, _, caught = _compute(_model(node))
        assert engine.unpriced_metrics == []
        assert caught == []

    def test_zero_quantity_is_not_reported(self):
        node = copy.deepcopy(FLAT_NODE)
        node["usageMetrics"]["gbOut"]["value"] = 0
        engine, _, caught = _compute(_model(node))
        assert engine.unpriced_metrics == []
        assert caught == []

    def test_quantity_is_in_the_output_time_basis(self):
        node = copy.deepcopy(FLAT_NODE)
        node["usageMetrics"]["seats"] = {"unit": "seats", "value": 25, "fixed": True}
        engine, _, _ = _compute(_model(node), time_basis="monthly")
        by_metric = {u.metric: u for u in engine.unpriced_metrics}
        assert by_metric["gbOut"].quantity == pytest.approx(3 * SECONDS_PER_MONTH)
        assert by_metric["seats"].quantity == pytest.approx(25)
        assert by_metric["seats"].time_basis == "monthly"

    def test_tiered_node_without_price_is_recorded(self):
        node = copy.deepcopy(FLAT_NODE)
        node["pricingModel"] = "tiered"
        engine, _, _ = _compute(_model(node))
        assert [u.metric for u in engine.unpriced_metrics] == ["gbOut"]

    def test_token_node_without_price_is_recorded(self):
        node = {
            "nodeType": "llm",
            "resourceAddress": "svc.api",
            "provider": "acme",
            "service": "Chat",
            "region": "global",
            "pricingModel": "token_based",
            "usageMetrics": {"inputTokens": 100, "outputTokens": 50},
            "pricingRates": {"inputTokens": 0.001},
        }
        engine, _, _ = _compute(_model(node))
        assert [u.metric for u in engine.unpriced_metrics] == ["outputTokens"]

    def test_catalog_miss_is_recorded(self, tmp_path):
        # The catalog has no row for this provider and region, and the node
        # has no pricingRates to fall back on (the issue #266 shape).
        node = copy.deepcopy(FLAT_NODE)
        node["pricingRates"] = {}
        catalog = PricingCatalog(tmp_path / "pricing.db")
        engine, costs, _ = _compute(_model(node), catalog=catalog)
        assert costs["svc.api"] == 0.0
        assert sorted(u.metric for u in engine.unpriced_metrics) == ["gbOut", "requests"]

    def test_recompute_does_not_duplicate_records(self):
        engine, _, _ = _compute(_model(copy.deepcopy(FLAT_NODE)))
        with pytest.warns(UnpricedMetricWarning):
            engine.compute()
        assert len(engine.unpriced_metrics) == 1

    def test_multi_workflow_reports_each_metric_once(self):
        model = _model(copy.deepcopy(FLAT_NODE))
        workflow = model.pop("workflow")
        second = copy.deepcopy(workflow)
        second["name"] = "second"
        model["workflows"] = [workflow, second]
        engine, _, _ = _compute(model)
        assert len(engine.unpriced_metrics) == 1
        assert engine.unpriced_metrics[0].quantity == pytest.approx(6.0)


def _write(tmp_path, model) -> str:
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(model))
    return str(path)


class TestCliReportsUnpricedMetrics:
    def test_compute_warns_on_stderr_and_exits_zero(self, tmp_path, capsys):
        path = _write(tmp_path, _model(copy.deepcopy(FLAT_NODE)))
        assert main(["compute", "--no-catalog", path]) == 0
        captured = capsys.readouterr()
        assert "svc.api" in captured.err and "gbOut" in captured.err
        assert "acme" in captured.err and "global" in captured.err
        assert "gbOut" not in captured.out

    def test_compute_exit_on_unpriced_fails(self, tmp_path, capsys):
        path = _write(tmp_path, _model(copy.deepcopy(FLAT_NODE)))
        assert main(["compute", "--no-catalog", "--exit-on-unpriced", path]) == 1
        assert "gbOut" in capsys.readouterr().err

    def test_compute_exit_on_unpriced_passes_when_all_priced(self, tmp_path, capsys):
        node = copy.deepcopy(FLAT_NODE)
        node["pricingRates"]["gbOut"] = 0.1
        path = _write(tmp_path, _model(node))
        assert main(["compute", "--no-catalog", "--exit-on-unpriced", path]) == 0
        assert capsys.readouterr().err == ""

    def test_analyze_json_lists_unpriced_metrics(self, tmp_path, capsys):
        path = _write(tmp_path, _model(copy.deepcopy(FLAT_NODE)))
        assert main(["analyze", "--json", path]) == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["unpriced_metrics"] == [{
            "node": "svc.api", "metric": "gbOut", "provider": "acme",
            "service": "Widgets", "region": "global",
            "quantity": pytest.approx(3 * SECONDS_PER_MONTH),
            "time_basis": "monthly",
        }]
        assert "gbOut" in captured.err

    def test_analyze_exit_on_unpriced_fails(self, tmp_path, capsys):
        path = _write(tmp_path, _model(copy.deepcopy(FLAT_NODE)))
        assert main(["analyze", "--exit-on-unpriced", path]) == 1

    def test_sensitivity_warns_once_per_metric(self, tmp_path, capsys):
        path = _write(tmp_path, _model(copy.deepcopy(FLAT_NODE)))
        assert main(["sensitivity", path, "--parameter", "frequency", "--steps", "3"]) == 0
        err = capsys.readouterr().err
        assert err.count("gbOut") == 1

    def test_what_if_sweep_warns(self, tmp_path, capsys):
        path = _write(tmp_path, _model(copy.deepcopy(FLAT_NODE)))
        assert main(["what-if", path, "--param", "frequency", "--values", "1,2",
                     "--output", "json"]) == 0
        captured = capsys.readouterr()
        json.loads(captured.out)
        assert captured.err.count("gbOut") == 1

    def test_other_engine_warnings_still_reach_stderr(self, tmp_path, capsys):
        model = _model(copy.deepcopy(FLAT_NODE))
        model["nodes"]["svc.orphan"] = {
            "nodeType": "compute", "resourceAddress": "svc.orphan",
            "usageMetrics": {"requests": 1}, "pricingRates": {"requests": 1.0},
        }
        path = _write(tmp_path, model)
        with pytest.warns(UserWarning, match="unreachable"):
            assert main(["compute", "--no-catalog", path]) == 0
