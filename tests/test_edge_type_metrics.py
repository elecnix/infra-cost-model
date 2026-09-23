"""A usage metric can count only the calls that arrive over one edge type (#313).

A DynamoDB table that declares both `Dynamo-ReadRequest` and
`Dynamo-WriteRequest` used to charge every incoming call as a read and as a
write. A metric now declares `edgeType: read` (or `write`, or `invoke`) and
counts only the calls that arrive over edges of that type. A metric without
`edgeType` still counts every call, and an edge without `type` is an
`invoke` edge, as the schema says.
"""

import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine, WorkloadDeriver
from infra_cost_model.schema import validate_cost_model

SECONDS_PER_MONTH = 2_629_800
READ_RATE = 1.25e-6
WRITE_RATE = 6.25e-6


def table_model(read_rate=0.7, write_rate=0.3, metrics=None, edge_types=("read", "write")):
    """An API that reads through one function and writes through another."""
    read_type, write_type = edge_types
    read_edge = {"from": "reader", "to": "table", "rate": 1}
    write_edge = {"from": "writer", "to": "table", "rate": 1}
    if read_type:
        read_edge["type"] = read_type
    if write_type:
        write_edge["type"] = write_type
    if metrics is None:
        metrics = {
            "Dynamo-ReadRequest": {"unit": "requests", "value": 1, "edgeType": "read"},
            "Dynamo-WriteRequest": {"unit": "requests", "value": 1, "edgeType": "write"},
        }
    return {
        "version": "1.0",
        "workflow": {
            "name": "items",
            "entry": "api",
            "frequency": {"unit": "perSecond", "value": 10},
        },
        "nodes": {
            "api": {"nodeType": "routing", "resourceAddress": "api"},
            "reader": {"nodeType": "compute", "resourceAddress": "reader"},
            "writer": {"nodeType": "compute", "resourceAddress": "writer"},
            "table": {
                "nodeType": "storage",
                "resourceAddress": "table",
                "usageMetrics": metrics,
                "pricingRates": {
                    "Dynamo-ReadRequest": READ_RATE,
                    "Dynamo-WriteRequest": WRITE_RATE,
                },
            },
        },
        "edges": [
            {"from": "api", "to": "reader", "rate": read_rate},
            {"from": "api", "to": "writer", "rate": write_rate},
            read_edge,
            write_edge,
        ],
    }


def derive(model):
    deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
    return deriver.derive()


def table_cost(model):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return CostEngine(model).compute()["table"]


class TestDerivation:
    def test_calls_are_counted_per_edge_type(self):
        usage = derive(table_model())["table"]
        assert usage.invocation_count == pytest.approx(10.0)
        assert usage.invocations_by_edge_type == pytest.approx({"read": 7.0, "write": 3.0})

    def test_a_metric_counts_the_calls_of_its_edge_type(self):
        usage = derive(table_model())["table"]
        assert usage.invocations_for({"value": 1, "edgeType": "read"}) == pytest.approx(7.0)
        assert usage.invocations_for({"value": 1, "edgeType": "write"}) == pytest.approx(3.0)

    def test_a_metric_without_edge_type_counts_every_call(self):
        usage = derive(table_model())["table"]
        assert usage.invocations_for({"value": 1}) == pytest.approx(10.0)
        assert usage.invocations_for(1) == pytest.approx(10.0)

    def test_an_edge_without_type_is_an_invoke_edge(self):
        usage = derive(table_model(edge_types=(None, "write")))["table"]
        assert usage.invocations_by_edge_type == pytest.approx({"invoke": 7.0, "write": 3.0})
        assert usage.invocations_for({"value": 1, "edgeType": "invoke"}) == pytest.approx(7.0)
        assert usage.invocations_for({"value": 1, "edgeType": "read"}) == 0.0

    def test_entry_traffic_counts_as_invoke(self):
        usage = derive(table_model())["api"]
        assert usage.invocations_by_edge_type == pytest.approx({"invoke": 10.0})

    def test_an_unknown_edge_type_is_refused(self):
        usage = derive(table_model())["table"]
        with pytest.raises(ValueError, match="edgeType 'reed'"):
            usage.invocations_for({"value": 1, "edgeType": "reed"})


class TestPricing:
    def test_reads_and_writes_are_charged_once_each(self):
        # 7 reads and 3 writes a second, not 10 of each.
        expected = 7 * READ_RATE + 3 * WRITE_RATE
        assert table_cost(table_model()) == pytest.approx(expected)

    def test_metrics_without_edge_type_keep_charging_every_call(self):
        metrics = {
            "Dynamo-ReadRequest": {"unit": "requests", "value": 1},
            "Dynamo-WriteRequest": {"unit": "requests", "value": 1},
        }
        expected = 10 * (READ_RATE + WRITE_RATE)
        assert table_cost(table_model(metrics=metrics)) == pytest.approx(expected)

    def test_untyped_edges_keep_charging_every_call(self):
        metrics = {
            "Dynamo-ReadRequest": {"unit": "requests", "value": 1},
            "Dynamo-WriteRequest": {"unit": "requests", "value": 1},
        }
        model = table_model(metrics=metrics, edge_types=(None, None))
        assert table_cost(model) == pytest.approx(10 * (READ_RATE + WRITE_RATE))

    def test_tiered_pricing_counts_calls_per_edge_type(self):
        model = table_model()
        model["nodes"]["table"]["pricingModel"] = "tiered"
        assert table_cost(model) == pytest.approx(7 * READ_RATE + 3 * WRITE_RATE)

    def test_a_fixed_metric_ignores_edge_type(self):
        metrics = {
            "Dynamo-ReadRequest": {"unit": "requests", "value": 1, "edgeType": "read"},
            "Dynamo-WriteRequest": {"unit": "requests", "value": 1000, "fixed": True,
                                    "edgeType": "write"},
        }
        model = table_model(metrics=metrics)
        model["nodes"]["table"]["pricingRates"]["Dynamo-WriteRequest"] = 0.01
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cost = CostEngine(model, time_basis="monthly").compute()["table"]
        assert cost == pytest.approx(7 * READ_RATE * SECONDS_PER_MONTH + 1000 * 0.01)

    def test_the_seed_catalog_prices_reads_and_writes_once(self, seed_catalog):
        # 7 reads and 3 writes a second cost the same as 10 calls that each
        # make 0.7 reads and 0.3 writes, whatever the seed prices are.
        def priced(metrics, edge_types):
            model = table_model(metrics=metrics, edge_types=edge_types)
            table = model["nodes"]["table"]
            del table["pricingRates"]
            table.update(provider="aws", region="us-east-1", service="AmazonDynamoDB",
                         resourceAddress="aws_dynamodb_table.items")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return CostEngine(model, catalog=seed_catalog,
                                  time_basis="monthly").compute()["table"]

        by_edge_type = priced(None, ("read", "write"))
        by_share = priced({
            "Dynamo-ReadRequest": {"unit": "requests", "value": 0.7},
            "Dynamo-WriteRequest": {"unit": "requests", "value": 0.3},
        }, (None, None))
        assert by_edge_type > 0
        assert by_edge_type == pytest.approx(by_share)

    def test_a_handler_quantity_counts_the_calls_of_its_edge_type(self, seed_catalog):
        # Lambda bills requests and GB-seconds, which its handler derives from
        # three metrics. When `invocations` counts only read calls, both
        # quantities cover only those calls: the same as a metric value of 0.7.
        def priced(invocations, edge_types):
            model = table_model(edge_types=edge_types)
            model["nodes"]["table"] = {
                "nodeType": "compute",
                "resourceAddress": "aws_lambda_function.table",
                "provider": "aws", "region": "us-east-1", "service": "AWSLambda",
                "usageMetrics": {
                    "invocations": invocations,
                    "avgDurationMs": {"unit": "ms", "value": 100_000},
                    "memoryMb": {"unit": "MB", "value": 1024},
                },
            }
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                engine = CostEngine(model, catalog=seed_catalog, time_basis="monthly")
                return engine.compute()["table"]

        by_edge_type = priced({"unit": "requests", "value": 1, "edgeType": "read"},
                              ("read", "write"))
        by_share = priced({"unit": "requests", "value": 0.7}, (None, None))
        assert by_edge_type > 0
        assert by_edge_type == pytest.approx(by_share)


class TestMultipleWorkflows:
    def model(self):
        model = table_model()
        workflow = model.pop("workflow")
        model["workflows"] = [
            {**workflow, "name": "reads", "entry": "reader",
             "frequency": {"unit": "perSecond", "value": 4}},
            {**workflow, "name": "writes", "entry": "writer",
             "frequency": {"unit": "perSecond", "value": 2}},
        ]
        return model

    def test_each_workflow_charges_its_own_edge_type(self):
        # 4 reads a second from one workflow, 2 writes from the other.
        assert table_cost(self.model()) == pytest.approx(4 * READ_RATE + 2 * WRITE_RATE)

    def test_merged_usage_keeps_the_counts_per_edge_type(self):
        engine = CostEngine(self.model())
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            engine.compute()
        usage = engine.get_derived_usage()["table"]
        assert usage.invocations_by_edge_type == pytest.approx({"read": 4.0, "write": 2.0})


class TestSchema:
    def test_edge_type_is_a_valid_usage_metric_key(self):
        assert validate_cost_model(table_model()) == []

    def test_an_unknown_edge_type_fails_validation(self):
        metrics = {"Dynamo-ReadRequest": {"unit": "requests", "value": 1, "edgeType": "reed"}}
        errors = validate_cost_model(table_model(metrics=metrics))
        assert any("edgeType" in e or "reed" in e for e in errors), errors


class TestSurfaces:
    """The Python surfaces carry edgeType the way the TypeScript ones do."""

    def test_node_usage_adds_a_metric_for_one_edge_type(self):
        from infra_cost_model.sdk.workflow import NodeUsage

        usage = (NodeUsage()
                 .with_metric("Dynamo-ReadRequest", 1, "requests", edge_type="read")
                 .with_metric("Dynamo-WriteRequest", 1, edge_type="write"))
        assert usage.metrics == {
            "Dynamo-ReadRequest": {"value": 1, "unit": "requests", "edgeType": "read"},
            "Dynamo-WriteRequest": {"value": 1, "edgeType": "write"},
        }

    def test_the_yaml_parser_keeps_edge_type(self):
        from pathlib import Path

        from infra_cost_model.sdk.workflow import parse_yaml_dsl

        path = Path(__file__).resolve().parent.parent / "examples" / "serverless-api.yaml"
        metrics = parse_yaml_dsl(path.read_text())["nodes"]["aws_dynamodb_table.items"][
            "usageMetrics"]
        assert metrics["Dynamo-ReadRequest"]["edgeType"] == "read"
        assert metrics["Dynamo-WriteRequest"]["edgeType"] == "write"
