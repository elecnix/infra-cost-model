"""Integration test for Stack: E-commerce Microservices (Issue #8).

Validates:
- Shared-node accumulation (DynamoDB orders from two Lambda paths)
- External percentage pricing (Stripe 2.9% + $0.30)
- Conditional branching (60/40 auth split, 95% fulfillment)
- Multi-hop depth (4 hops: auth → create_order → SQS → process_payment → fulfill_order)
- SQS decoupling and Kahn's algorithm correctness
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


class TestEcommerceModel:
    """Validate the e-commerce YAML model."""

    def test_model_loads(self):
        model = load_yaml_model("ecommerce-microservices.yaml")
        assert model["version"] == "1.0"
        assert model["workflow"]["name"] == "ecommerce-api"

    def test_frequency_10_per_minute(self):
        model = load_yaml_model("ecommerce-microservices.yaml")
        freq = model["workflow"]["frequency"]
        assert freq["unit"] == "perMinute"
        assert freq["value"] == 10

    def test_stripe_percentage_pricing(self):
        model = load_yaml_model("ecommerce-microservices.yaml")
        stripe = model["nodes"]["external_api.stripe"]
        assert stripe["pricingModel"] == "percentage"
        assert stripe["pricingRates"]["percentageRate"] == 0.029
        assert stripe["pricingRates"]["fixedPerTransaction"] == 0.30

    def test_auth_branching_rates(self):
        model = load_yaml_model("ecommerce-microservices.yaml")
        # Auth → get_products: 0.6, Auth → create_order: 0.4
        edges = model["edges"]
        get_products_edges = [
            e for e in edges
            if e["from"] == "aws_lambda_function.auth"
            and e["to"] == "aws_lambda_function.get_products"
        ]
        create_order_edges = [
            e for e in edges
            if e["from"] == "aws_lambda_function.auth"
            and e["to"] == "aws_lambda_function.create_order"
        ]
        assert get_products_edges[0]["rate"] == 0.6
        assert create_order_edges[0]["rate"] == 0.4

    def test_conditional_fulfillment(self):
        model = load_yaml_model("ecommerce-microservices.yaml")
        # process_payment → fulfill_order: 0.95 (5% failures)
        edges = [
            e for e in model["edges"]
            if e["from"] == "aws_lambda_function.process_payment"
            and e["to"] == "aws_lambda_function.fulfill_order"
        ]
        assert edges[0]["rate"] == 0.95

    def test_all_nodes_defined(self):
        model = load_yaml_model("ecommerce-microservices.yaml")
        expected = {
            "aws_apigatewayv2_api.ecommerce_api",
            "data_transfer.ecommerce_api_egress",
            "aws_lambda_function.auth",
            "aws_lambda_function.get_products",
            "aws_lambda_function.create_order",
            "aws_lambda_function.process_payment",
            "aws_lambda_function.fulfill_order",
            "aws_dynamodb_table.products",
            "aws_dynamodb_table.orders",
            "aws_sqs_queue.order_queue",
            "external_api.stripe",
            "aws_sns_topic.notifications",
        }
        assert set(model["nodes"].keys()) == expected

    def test_dag_no_cycles(self):
        model = load_yaml_model("ecommerce-microservices.yaml")
        from infra_cost_model.engine.engine import DAGValidator
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is True


class TestSharedNodeAccumulation:
    """Validates multi-path accumulation on shared DynamoDB orders table."""

    @pytest.fixture
    def engine(self, seed_catalog):
        model = load_yaml_model("ecommerce-microservices.yaml")
        return CostEngine(model, catalog=seed_catalog)

    def test_orders_has_writes_from_both_paths(self, engine):
        """DynamoDB orders receives writes from create_order AND fulfill_order."""
        engine.compute()
        derived = engine.derived_usage

        orders = derived["aws_dynamodb_table.orders"]
        # Edge types should include 'write'
        assert "write" in orders.edge_types

    def test_create_order_path_invocations(self, engine):
        """create_order Lambda gets 40% of auth traffic."""
        engine.compute()
        derived = engine.derived_usage

        auth = derived["aws_lambda_function.auth"]
        create_order = derived["aws_lambda_function.create_order"]

        # create_order = auth * 0.4
        assert create_order.invocation_count == pytest.approx(auth.invocation_count * 0.4)

    def test_get_products_path_invocations(self, engine):
        """get_products Lambda gets 60% of auth traffic."""
        engine.compute()
        derived = engine.derived_usage

        auth = derived["aws_lambda_function.auth"]
        get_products = derived["aws_lambda_function.get_products"]

        assert get_products.invocation_count == pytest.approx(auth.invocation_count * 0.6)

    def test_fulfill_order_receives_95_percent(self, engine):
        """fulfill_order Lambda gets 95% of process_payment traffic."""
        engine.compute()
        derived = engine.derived_usage

        process_payment = derived["aws_lambda_function.process_payment"]
        fulfill = derived["aws_lambda_function.fulfill_order"]

        assert fulfill.invocation_count == pytest.approx(process_payment.invocation_count * 0.95)

    def test_branching_sums_to_entry(self, engine):
        """Auth branching: 0.6 + 0.4 = 1.0 of auth traffic to children."""
        engine.compute()
        derived = engine.derived_usage

        auth = derived["aws_lambda_function.auth"]
        get_products = derived["aws_lambda_function.get_products"]
        create_order = derived["aws_lambda_function.create_order"]

        # Sum of child invocations should equal parent * branching rates
        expected_children = auth.invocation_count * (0.6 + 0.4)
        actual_children = get_products.invocation_count + create_order.invocation_count
        assert actual_children == pytest.approx(expected_children)

    def test_orders_accumulates_from_multiple_paths(self, engine):
        """DynamoDB orders table accumulates writes from both Lambda paths.

        create_order (rate=1, 40% of auth) + fulfill_order (rate=0.95, at process_payment rate)
        = auth * 0.4 * 1 + auth * 0.4 * 1 * 1 * 0.95
        = auth * 0.4 * (1 + 0.95) = auth * 0.4 * 1.95 = auth * 0.78
        """
        engine.compute()
        derived = engine.derived_usage

        auth = derived["aws_lambda_function.auth"]
        orders = derived["aws_dynamodb_table.orders"]

        expected = auth.invocation_count * 0.4 * (1.0 + 0.95)
        assert orders.invocation_count == pytest.approx(expected, rel=0.01)

    def test_multi_hop_depth(self, engine):
        """Verify 4-hop depth: auth → create_order → SQS → process_payment → fulfill_order."""
        engine.compute()
        derived = engine.derived_usage

        auth = derived["aws_lambda_function.auth"]
        create_order = derived["aws_lambda_function.create_order"]
        sqs = derived["aws_sqs_queue.order_queue"]
        process_payment = derived["aws_lambda_function.process_payment"]
        fulfill = derived["aws_lambda_function.fulfill_order"]

        # All nodes in the 4-hop chain should be reachable
        assert create_order.invocation_count > 0
        assert sqs.invocation_count > 0
        assert process_payment.invocation_count > 0
        assert fulfill.invocation_count > 0

        # Each hop should have correct invocation counts
        assert create_order.invocation_count == pytest.approx(auth.invocation_count * 0.4)
        assert sqs.invocation_count == pytest.approx(create_order.invocation_count)
        assert process_payment.invocation_count == pytest.approx(sqs.invocation_count)
        assert fulfill.invocation_count == pytest.approx(process_payment.invocation_count * 0.95)


class TestStripePercentagePricing:
    """Validates Stripe external pricing (2.9% + $0.30)."""

    @pytest.fixture
    def engine(self, seed_catalog):
        model = load_yaml_model("ecommerce-microservices.yaml")
        return CostEngine(model, catalog=seed_catalog, time_basis="monthly")

    def test_stripe_cost_is_percentage_based(self, engine):
        """Stripe cost uses percentage model."""
        costs = engine.compute()
        assert "external_api.stripe" in costs
        assert costs["external_api.stripe"] > 0

    def test_stripe_cost_formula(self, engine):
        """Manual check: transactions × ($50 × 2.9% + $0.30) × seconds/month.

        transactionVolume is the value of one order (Principle 4), so both the
        percentage fee and the fixed fee apply to every transaction.
        """
        costs = engine.compute()
        invocations_per_sec = engine.derived_usage["external_api.stripe"].invocation_count

        expected_monthly = invocations_per_sec * (50 * 0.029 + 0.30) * 2629800

        assert costs["external_api.stripe"] == pytest.approx(expected_monthly, rel=0.01)

    def test_stripe_monthly_cost_is_plausible(self, engine):
        """10 req/min × 40% = 4 orders/min, about 175,000 orders a month.
        At $1.75 each, Stripe costs about $307,000 a month."""
        costs = engine.compute()
        assert costs["external_api.stripe"] == pytest.approx(
            10 / 60 * 0.4 * 1.75 * 2629800, rel=0.001)

    def test_stripe_transaction_count(self, engine):
        """Stripe receives one call per payment."""
        engine.compute()
        derived = engine.derived_usage

        process_payment = derived["aws_lambda_function.process_payment"]
        stripe = derived["external_api.stripe"]

        # Stripe is called once per process_payment invocation
        assert stripe.invocation_count == pytest.approx(process_payment.invocation_count)

    def test_stripe_monthly_volume(self, engine):
        """At 10 req/min with 40% creating an order, about 175,000 payments a month."""
        engine.compute()
        stripe = engine.derived_usage["external_api.stripe"]
        monthly_txns = stripe.invocation_count * 2629800

        assert monthly_txns == pytest.approx(10 / 60 * 0.4 * 2629800)


class TestCostBreakdown:
    """Validates cost computation and structure."""

    @pytest.fixture
    def engine(self, seed_catalog):
        model = load_yaml_model("ecommerce-microservices.yaml")
        return CostEngine(model, catalog=seed_catalog, time_basis="monthly")

    def test_total_cost_positive(self, engine):
        assert engine.total_cost() > 0

    def test_stripe_is_largest_cost(self, engine):
        """Stripe (percentage of volume) should dominate."""
        costs = engine.compute()
        total = engine.total_cost()
        assert costs["external_api.stripe"] / total > 0.9

    def test_orders_table_cost(self, engine):
        """DynamoDB orders cost from combined writes."""
        costs = engine.compute()
        assert costs["aws_dynamodb_table.orders"] > 0

    def test_sqs_cost(self, engine):
        """175,320 messages a month are inside the SQS free tier of 1M requests."""
        costs = engine.compute()
        assert costs["aws_sqs_queue.order_queue"] == 0

    def test_sns_cost(self, engine):
        """166,554 publishes a month are inside the SNS free tier of 1M requests."""
        costs = engine.compute()
        assert costs["aws_sns_topic.notifications"] == 0
