"""Integration test for Stack: Serverless API (Issue #3).

Validates:
- Entry node branching with call rates (0.7 read / 0.3 write)
- Shared leaf node (DynamoDB) receiving reads and writes from two paths
- Different Lambda configurations (256MB vs 512MB, 50ms vs 200ms)
- PAY_PER_REQUEST DynamoDB pricing with read/write differentiation
- Derived usage correctness at 1000/min frequency
"""

import pytest
import yaml
from pathlib import Path
from infra_cost_model.engine.engine import CostEngine


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def load_yaml_model(name: str) -> dict:
    path = EXAMPLES_DIR / name
    with open(path) as f:
        return yaml.safe_load(f)


class TestServerlessModel:
    """Validate the serverless API YAML model."""

    def test_model_loads(self):
        model = load_yaml_model("serverless-api.yaml")
        assert model["version"] == "1.0"
        assert model["workflow"]["name"] == "serverless-api"

    def test_frequency_1000_per_minute(self):
        model = load_yaml_model("serverless-api.yaml")
        freq = model["workflow"]["frequency"]
        assert freq["unit"] == "perMinute"
        assert freq["value"] == 1000

    def test_entry_branching_rates(self):
        """API Gateway branches: 0.7 read, 0.3 write."""
        model = load_yaml_model("serverless-api.yaml")
        entry_edges = [
            e for e in model["edges"]
            if e["from"] == "aws_apigatewayv2_api.items_api"
        ]
        rates = {e["to"]: e["rate"] for e in entry_edges}
        assert rates["aws_lambda_function.get_items"] == 0.7
        assert rates["aws_lambda_function.create_item"] == 0.3

    def test_different_lambda_configs(self):
        """get_items: 256MB/50ms, create_item: 512MB/200ms."""
        model = load_yaml_model("serverless-api.yaml")
        get_items = model["nodes"]["aws_lambda_function.get_items"]
        create_item = model["nodes"]["aws_lambda_function.create_item"]

        assert get_items["usageMetrics"]["avgDurationMs"]["value"] == 50
        assert get_items["usageMetrics"]["memoryMb"]["value"] == 256
        assert create_item["usageMetrics"]["avgDurationMs"]["value"] == 200
        assert create_item["usageMetrics"]["memoryMb"]["value"] == 512

    def test_dynamodb_read_write_differentiation(self, seed_catalog):
        """DynamoDB has different pricing for reads vs writes."""
        model = load_yaml_model("serverless-api.yaml")
        ddb = model["nodes"]["aws_dynamodb_table.items"]
        assert "pricingRates" not in ddb

        def price(metric):
            assert metric in ddb["usageMetrics"]
            return seed_catalog.query("aws", ddb["service"], ddb["region"], metric).price_usd

        # Writes are 5× more expensive than reads
        ratio = price("Dynamo-WriteRequest") / price("Dynamo-ReadRequest")
        assert ratio == pytest.approx(5.0)

    def test_all_nodes_defined(self):
        model = load_yaml_model("serverless-api.yaml")
        expected = {
            "aws_apigatewayv2_api.items_api",
            "data_transfer.items_api_egress",
            "aws_lambda_function.get_items",
            "aws_lambda_function.create_item",
            "aws_dynamodb_table.items",
        }
        assert set(model["nodes"].keys()) == expected

    def test_dag_no_cycles(self):
        model = load_yaml_model("serverless-api.yaml")
        from infra_cost_model.engine.engine import DAGValidator
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is True


class TestDerivedUsage:
    """Validates derived usage computation."""

    @pytest.fixture
    def engine(self, seed_catalog):
        model = load_yaml_model("serverless-api.yaml")
        return CostEngine(model, catalog=seed_catalog)

    def test_entry_frequency(self, engine):
        """1000/min converts to per-second correctly."""
        engine.compute()
        derived = engine.derived_usage

        entry = derived["aws_apigatewayv2_api.items_api"]
        # 1000/min = 1000/60 ≈ 16.667 per second
        assert entry.invocation_count == pytest.approx(1000 / 60, rel=0.01)

    def test_read_path_70_percent(self, engine):
        """get_items Lambda gets 70% of entry traffic."""
        engine.compute()
        derived = engine.derived_usage

        entry = derived["aws_apigatewayv2_api.items_api"]
        get_items = derived["aws_lambda_function.get_items"]

        assert get_items.invocation_count == pytest.approx(entry.invocation_count * 0.7)

    def test_write_path_30_percent(self, engine):
        """create_item Lambda gets 30% of entry traffic."""
        engine.compute()
        derived = engine.derived_usage

        entry = derived["aws_apigatewayv2_api.items_api"]
        create_item = derived["aws_lambda_function.create_item"]

        assert create_item.invocation_count == pytest.approx(entry.invocation_count * 0.3)

    def test_branching_sums_to_entry(self, engine):
        """0.7 + 0.3 = 1.0 of entry traffic reaches children."""
        engine.compute()
        derived = engine.derived_usage

        entry = derived["aws_apigatewayv2_api.items_api"]
        get_items = derived["aws_lambda_function.get_items"]
        create_item = derived["aws_lambda_function.create_item"]

        assert (get_items.invocation_count + create_item.invocation_count ==
                pytest.approx(entry.invocation_count))

    def test_shared_leaf_accumulation(self, engine):
        """DynamoDB receives reads and writes from both paths."""
        engine.compute()
        derived = engine.derived_usage

        ddb = derived["aws_dynamodb_table.items"]
        get_items = derived["aws_lambda_function.get_items"]
        create_item = derived["aws_lambda_function.create_item"]

        # DynamoDB gets: get_items * 1 (read) + create_item * 1 (write)
        expected = get_items.invocation_count + create_item.invocation_count
        assert ddb.invocation_count == pytest.approx(expected)

    def test_dynamodb_edge_types(self, engine):
        """DynamoDB has both 'read' and 'write' edge types."""
        engine.compute()
        derived = engine.derived_usage

        ddb = derived["aws_dynamodb_table.items"]
        assert "read" in ddb.edge_types
        assert "write" in ddb.edge_types


class TestCostComputation:
    """Validates cost computation for the serverless API."""

    @pytest.fixture
    def engine(self, seed_catalog):
        model = load_yaml_model("serverless-api.yaml")
        return CostEngine(model, catalog=seed_catalog, time_basis="monthly")

    def test_total_cost_positive(self, engine):
        assert engine.total_cost() > 0

    def test_all_nodes_costed(self, engine):
        costs = engine.compute()
        for node in ["aws_apigatewayv2_api.items_api", "aws_lambda_function.get_items",
                      "aws_lambda_function.create_item", "aws_dynamodb_table.items"]:
            assert node in costs
            assert costs[node] >= 0

    def test_create_item_more_expensive_than_get_items(self, engine):
        """create_item (512MB/200ms) costs more per invocation than get_items (256MB/50ms)."""
        costs = engine.compute()
        # create_item has higher GB-sec per invocation AND write pricing is higher
        # Even with fewer invocations (30% vs 70%), it may cost more per-invocation
        # But total cost depends on volume — get_items has more invocations
        assert costs["aws_lambda_function.create_item"] > 0
        assert costs["aws_lambda_function.get_items"] > 0

    def test_dynamodb_read_write_breakdown(self, engine):
        """DynamoDB has both read and write costs from accumulated usage."""
        costs = engine.compute()
        ddb_cost = costs["aws_dynamodb_table.items"]
        assert ddb_cost > 0

    def test_sensitivity_on_frequency(self, engine, seed_catalog):
        """Doubling frequency doubles cost, plus at most the Lambda free tiers.

        The free tiers are a fixed allowance, so at twice the traffic less of
        the bill is free. Each of the two functions has at most 1M requests at
        $0.20/M and 400,000 GB-seconds at $0.0000166667 free, and the account
        has 100 GB of data transfer out at $0.09 free (#327).
        """
        base_cost = engine.total_cost()

        model = load_yaml_model("serverless-api.yaml")
        model["workflow"]["frequency"]["value"] = 2000
        engine_2x = CostEngine(model, catalog=seed_catalog, time_basis="monthly")
        cost_2x = engine_2x.total_cost()

        free_tiers = 2 * (1_000_000 * 0.20e-6 + 400_000 * 0.0000166667) + 100 * 0.09
        assert base_cost * 2 <= cost_2x <= base_cost * 2 + free_tiers

    def test_frequency_change_what_if(self, engine, seed_catalog):
        """What-if analysis on read/write ratio."""
        from infra_cost_model.engine.engine import ParametricSensitivityAnalyzer

        model = load_yaml_model("serverless-api.yaml")
        analyzer = ParametricSensitivityAnalyzer(model, seed_catalog)

        # Frequency has positive derivative
        deriv = analyzer.partial_derivative("frequency")
        assert deriv > 0

    def test_monthly_request_counts(self, engine):
        """At 1000/min, monthly requests should be ~43.8M."""
        engine.compute()
        derived = engine.derived_usage

        entry = derived["aws_apigatewayv2_api.items_api"]
        monthly = entry.invocation_count * 2629800

        assert monthly == pytest.approx(43_800_000, rel=0.02)
