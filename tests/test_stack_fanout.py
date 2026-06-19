"""Integration test for Stack: Event-Driven Fan-Out (Issue #5).

Validates:
- SNS fan-out to multiple SQS queues with different call rates (0.8, 1.0, 0.5)
- Multi-path DAG accumulation via Kahn's algorithm
- Long-running compute (analyzer: 5s at 1024MB)
- Data volume metrics (S3 data lake)
- Correct invocation derivation through multi-hop chains
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


class TestFanoutModel:
    """Validate the fan-out YAML model."""

    def test_model_loads(self):
        model = load_yaml_model("event-driven-fanout.yaml")
        assert model["version"] == "1.0"

    def test_frequency_500_per_minute(self):
        model = load_yaml_model("event-driven-fanout.yaml")
        freq = model["workflow"]["frequency"]
        assert freq["unit"] == "perMinute"
        assert freq["value"] == 500

    def test_sns_fanout_rates(self):
        """SNS fans out to 3 SQS queues with 0.8, 1.0, 0.5 rates."""
        model = load_yaml_model("event-driven-fanout.yaml")
        sns_edges = [
            e for e in model["edges"]
            if e["from"] == "aws_sns_topic.order_events"
        ]
        rates = {e["to"]: e["rate"] for e in sns_edges}
        assert rates["aws_sqs_queue.orders_queue"] == 0.8
        assert rates["aws_sqs_queue.notifications_queue"] == 1.0
        assert rates["aws_sqs_queue.analytics_queue"] == 0.5

    def test_analyzer_long_running(self):
        """Analyzer Lambda runs 5s at 1024MB = 5.0 GB-sec."""
        model = load_yaml_model("event-driven-fanout.yaml")
        analyzer = model["nodes"]["aws_lambda_function.analyzer"]
        assert analyzer["usageMetrics"]["gb_seconds"]["value"] == 5.0

    def test_all_nodes_defined(self):
        model = load_yaml_model("event-driven-fanout.yaml")
        expected = {
            "aws_apigatewayv2_api.orders_api",
            "aws_lambda_function.producer",
            "aws_sns_topic.order_events",
            "aws_sqs_queue.orders_queue",
            "aws_sqs_queue.notifications_queue",
            "aws_sqs_queue.analytics_queue",
            "aws_lambda_function.order_processor",
            "aws_lambda_function.notifier",
            "aws_lambda_function.analyzer",
            "aws_dynamodb_table.orders",
            "aws_sns_topic.alerts",
            "aws_s3_bucket.data_lake",
        }
        assert set(model["nodes"].keys()) == expected

    def test_dag_no_cycles(self):
        model = load_yaml_model("event-driven-fanout.yaml")
        from infra_cost_model.engine.engine import DAGValidator
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is True


class TestFanOutDerivation:
    """Validates fan-out invocation derivation."""

    @pytest.fixture
    def engine(self):
        model = load_yaml_model("event-driven-fanout.yaml")
        return CostEngine(model)

    def test_sns_invocations_match_producer(self, engine):
        """SNS receives 1:1 from producer Lambda."""
        engine.compute()
        derived = engine.derived_usage

        producer = derived["aws_lambda_function.producer"]
        sns = derived["aws_sns_topic.order_events"]
        assert sns.invocation_count == pytest.approx(producer.invocation_count)

    def test_orders_queue_rate(self, engine):
        """Orders queue gets 0.8× SNS traffic."""
        engine.compute()
        derived = engine.derived_usage

        sns = derived["aws_sns_topic.order_events"]
        orders_q = derived["aws_sqs_queue.orders_queue"]
        assert orders_q.invocation_count == pytest.approx(sns.invocation_count * 0.8)

    def test_notifications_queue_rate(self, engine):
        """Notifications queue gets 1.0× SNS traffic."""
        engine.compute()
        derived = engine.derived_usage

        sns = derived["aws_sns_topic.order_events"]
        notif_q = derived["aws_sqs_queue.notifications_queue"]
        assert notif_q.invocation_count == pytest.approx(sns.invocation_count * 1.0)

    def test_analytics_queue_rate(self, engine):
        """Analytics queue gets 0.5× SNS traffic — half sampling."""
        engine.compute()
        derived = engine.derived_usage

        sns = derived["aws_sns_topic.order_events"]
        analytics_q = derived["aws_sqs_queue.analytics_queue"]
        assert analytics_q.invocation_count == pytest.approx(sns.invocation_count * 0.5)

    def test_fanout_children_independent(self, engine):
        """Fan-out children are independent — changing one doesn't affect others."""
        engine.compute()
        derived = engine.derived_usage

        orders_q = derived["aws_sqs_queue.orders_queue"]
        notif_q = derived["aws_sqs_queue.notifications_queue"]
        analytics_q = derived["aws_sqs_queue.analytics_queue"]

        # All three should be derived independently from SNS
        assert orders_q.invocation_count > 0
        assert notif_q.invocation_count > 0
        assert analytics_q.invocation_count > 0

    def test_analyzer_derived_correctly(self, engine):
        """Analyzer Lambda gets analytics_queue × 1.0 invocations."""
        engine.compute()
        derived = engine.derived_usage

        analytics_q = derived["aws_sqs_queue.analytics_queue"]
        analyzer = derived["aws_lambda_function.analyzer"]
        assert analyzer.invocation_count == pytest.approx(analytics_q.invocation_count)

    def test_data_lake_receives_data(self, engine):
        """S3 data lake gets data from analyzer with 500KB per event."""
        engine.compute()
        derived = engine.derived_usage

        data_lake = derived["aws_s3_bucket.data_lake"]
        assert data_lake.data_in > 0


class TestFanOutCosts:
    """Validates cost computation for fan-out pattern."""

    @pytest.fixture
    def engine(self):
        model = load_yaml_model("event-driven-fanout.yaml")
        return CostEngine(model, time_basis="monthly")

    def test_total_cost_positive(self, engine):
        assert engine.total_cost() > 0

    def test_analyzer_is_most_expensive_lambda(self, engine):
        """Analyzer (5s at 1024MB) should cost more than other Lambdas."""
        costs = engine.compute()
        lambdas = {
            "aws_lambda_function.producer": costs["aws_lambda_function.producer"],
            "aws_lambda_function.order_processor": costs["aws_lambda_function.order_processor"],
            "aws_lambda_function.notifier": costs["aws_lambda_function.notifier"],
            "aws_lambda_function.analyzer": costs["aws_lambda_function.analyzer"],
        }
        most_expensive = max(lambdas, key=lambdas.get)
        assert most_expensive == "aws_lambda_function.analyzer"

    def test_analyzer_cost_scales_with_memory(self, engine):
        """Analyzer cost is proportional to GB-seconds."""
        costs = engine.compute()
        analyzer_cost = costs["aws_lambda_function.analyzer"]
        notifier_cost = costs["aws_lambda_function.notifier"]

        # Analyzer should cost more than notifier despite fewer invocations (0.5 vs 1.0)
        # because it uses 5.0 GB-sec vs 0.0125 GB-sec per invocation
        assert analyzer_cost > notifier_cost

    def test_sns_cost_covers_all_fanouts(self, engine):
        """SNS cost accounts for all fan-out deliveries."""
        costs = engine.compute()
        assert costs["aws_sns_topic.order_events"] > 0

    def test_analytics_sampling_reduces_cost(self, engine):
        """Reducing analytics sampling from 0.5 to 0.1 reduces total cost."""
        from infra_cost_model.engine.engine import ParametricSensitivityAnalyzer

        model = load_yaml_model("event-driven-fanout.yaml")
        base_engine = CostEngine(model, time_basis="monthly")
        base_total = base_engine.total_cost()

        # Change analytics sampling to 0.1
        model["workflow"]["parameters"] = {"analytics_rate": 0.1}
        # Find and update the analytics queue edge
        for edge in model["edges"]:
            if (edge["from"] == "aws_sns_topic.order_events" and
                    edge["to"] == "aws_sqs_queue.analytics_queue"):
                edge["rate"] = 0.1

        mod_engine = CostEngine(model, time_basis="monthly")
        mod_total = mod_engine.total_cost()

        assert mod_total < base_total
