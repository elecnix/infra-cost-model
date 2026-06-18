"""Tests for cost engine module."""

import pytest
from infra_cost_model.engine.engine import (
    DAGValidator, WorkloadDeriver, CostAggregator, CostEngine, DerivedUsage,
)


def make_valid_cost_model(entry="api_gateway", frequency=100):
    """Helper to create a valid cost model structure."""
    return {
        "version": "1.0",
        "workflow": {
            "name": "test-workflow",
            "entry": entry,
            "frequency": {
                "unit": "perMinute",
                "value": frequency,
            }
        },
        "nodes": {
            "api_gateway": {
                "nodeType": "routing",
                "resourceAddress": "aws_api_gateway_rest_api.test_api",
                "provider": "aws",
                "service": "APIGateway",
            },
            "get_user_fn": {
                "nodeType": "compute",
                "resourceAddress": "aws_lambda_function.get_user",
                "provider": "aws",
                "service": "AWSLambda",
                "usageMetrics": {
                    "invocations": {"unit": "requests", "value": 1},
                    "avgDurationMs": {"unit": "ms", "value": 200},
                    "memoryMb": {"unit": "MB", "value": 256},
                },
                "pricingRates": {
                    "invocations": 0.20e-6,
                    "memoryDuration": 0.0000166667,
                }
            },
            "users_table": {
                "nodeType": "storage",
                "resourceAddress": "aws_dynamodb_table.users",
                "provider": "aws",
                "service": "AmazonDynamoDB",
                "usageMetrics": {
                    "readRequests": {"unit": "requests", "value": 1},
                },
                "pricingRates": {
                    "readRequests": 1.25e-6,
                }
            }
        },
        "edges": [
            {"from": "api_gateway", "to": "get_user_fn", "rate": 0.8},
            {"from": "api_gateway", "to": "users_table", "rate": 1.0},
            {"from": "get_user_fn", "to": "users_table", "rate": 1.0},
        ]
    }


class TestDAGValidator:
    """Tests for DAG validation."""
    
    def test_valid_dag(self):
        """Test that a valid DAG passes validation."""
        model = make_valid_cost_model()
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is True
        assert validator.errors == []
    
    def test_missing_from_node(self):
        """Test error when edge source doesn't exist."""
        model = make_valid_cost_model()
        model["edges"].append({"from": "nonexistent", "to": "get_user_fn", "rate": 1.0})
        
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is False
        assert any("not found" in e for e in validator.errors)
    
    def test_missing_to_node(self):
        """Test error when edge target doesn't exist."""
        model = make_valid_cost_model()
        model["edges"].append({"from": "api_gateway", "to": "nonexistent", "rate": 1.0})
        
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is False
        assert any("not found" in e for e in validator.errors)
    
    def test_cycle_detection(self):
        """Test that cycles are detected."""
        model = make_valid_cost_model()
        # Create a cycle: users_table -> api_gateway
        model["edges"].append({"from": "users_table", "to": "api_gateway", "rate": 1.0})
        
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is False
        assert any("Cycle detected" in e for e in validator.errors)


class TestWorkloadDeriver:
    """Tests for workload derivation."""
    
    def test_entry_frequency_conversion(self):
        """Test frequency conversion to per-second."""
        model = make_valid_cost_model(frequency=60)  # 60 per minute
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        deriver.derive()
        
        # Entry should have 1 per second (60/min / 60 = 1/sec)
        entry_usage = deriver.derived_usage["api_gateway"]
        assert entry_usage.invocation_count == pytest.approx(1.0)
    
    def test_invocation_propagation(self):
        """Test invocation propagation through edges."""
        model = make_valid_cost_model(frequency=100)  # 100 per minute = 100/60 per second
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        derived = deriver.derive()
        
        # api_gateway gets base frequency
        assert derived["api_gateway"].invocation_count == pytest.approx(100 / 60, rel=0.01)
        
        # get_user_fn gets 0.8 * api_gateway rate
        expected_fn = (100 / 60) * 0.8
        assert derived["get_user_fn"].invocation_count == pytest.approx(expected_fn, rel=0.01)
        
        # users_table gets: 0.8 (api->fn->table) + 1.0 (api->table directly)
        expected_table = (100 / 60) * (0.8 * 1.0 + 1.0)
        assert derived["users_table"].invocation_count == pytest.approx(expected_table, rel=0.01)
    
    def test_multi_path_dag_topological_order(self):
        """Test multi-path DAG: A→B, A→C, C→B, B→D.
        
        With BFS order, B may be dequeued and propagate to D before C's
        contribution to B arrives. Topological sort fixes this by only
        propagating B downstream after all incoming edges processed.
        """
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "entry", "resourceAddress": "entry"},
            "B": {"nodeType": "compute", "resourceAddress": "compute_b"},
            "C": {"nodeType": "compute", "resourceAddress": "compute_c"},
            "D": {"nodeType": "storage", "resourceAddress": "storage_d"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 1.0, "type": "invoke"},
            {"from": "A", "to": "C", "rate": 1.0, "type": "invoke"},
            {"from": "C", "to": "B", "rate": 1.0, "type": "invoke"},
            {"from": "B", "to": "D", "rate": 1.0, "type": "invoke"},
        ]
        
        deriver = WorkloadDeriver(workflow, nodes, edges)
        derived = deriver.derive()
        
        # A gets entry frequency = 10
        assert derived["A"].invocation_count == 10.0
        # C gets A * 1.0 = 10
        assert derived["C"].invocation_count == 10.0
        # B gets A * 1.0 + C * 1.0 = 10 + 10 = 20 (not 10!)
        assert derived["B"].invocation_count == 20.0, \
            f"Expected 20.0 (10 from A + 10 from C), got {derived['B'].invocation_count}"
        # D gets B * 1.0 = 20 (not 10!)
        assert derived["D"].invocation_count == 20.0, \
            f"Expected 20.0 (from B=20), got {derived['D'].invocation_count}"
    
    def test_per_second_frequency(self):
        """Test perSecond frequency conversion."""
        model = make_valid_cost_model()
        model["workflow"]["frequency"] = {"unit": "perSecond", "value": 10}
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        derived = deriver.derive()
        
        assert derived["api_gateway"].invocation_count == 10.0


class TestCostAggregator:
    """Tests for cost aggregation."""
    
    def test_aggregate_basic_cost(self):
        """Test basic cost aggregation."""
        model = make_valid_cost_model()
        derived = {
            "api_gateway": DerivedUsage("api_gateway", 6000.0),
            "get_user_fn": DerivedUsage("get_user_fn", 4800.0),
            "users_table": DerivedUsage("users_table", 10800.0),
        }
        
        aggregator = CostAggregator(model["nodes"], derived, model["edges"])
        costs = aggregator.aggregate()
        
        assert "api_gateway" in costs
        assert "get_user_fn" in costs
        assert "users_table" in costs
    
    def test_cost_uses_pricing_rates(self):
        """Test that costs multiply invocation_count × per-invocation value × rate."""
        nodes = {
            "test_fn": {
                "nodeType": "compute",
                "resourceAddress": "aws_lambda_function.test",
                "provider": "aws",
                "service": "AWSLambda",
                "usageMetrics": {
                    "invocations": {"unit": "requests", "value": 1},
                    "gb_seconds": {"unit": "GB-seconds", "value": 0.5},
                },
                "pricingRates": {
                    "invocations": 0.20e-6,  # $0.20 per million requests
                    "gb_seconds": 0.0000166667,  # $0.0000166667 per GB-second
                }
            }
        }
        derived = {"test_fn": DerivedUsage("test_fn", 1000.0)}
        
        aggregator = CostAggregator(nodes, derived, [])
        costs = aggregator.aggregate()
        
        # invocations: 1000 invocations × 1 × $0.20e-6 = $0.0002
        # gb_seconds: 1000 invocations × 0.5 GB-sec × $0.0000166667 = $0.00833...
        expected = 1000 * 1 * 0.20e-6 + 1000 * 0.5 * 0.0000166667
        assert costs["test_fn"] == pytest.approx(expected)
    
    def test_percentage_pricing_cost(self):
        """Test percentage-based pricing (e.g., Stripe 2.9% + $0.30)."""
        nodes = {
            "stripe": {
                "nodeType": "external",
                "resourceAddress": "external.stripe_payments",
                "pricingModel": "percentage",
                "pricingRates": {
                    "percentageRate": 0.029,
                    "fixedPerTransaction": 0.30,
                },
                "usageMetrics": {
                    "transactionVolume": {"value": 10000},
                }
            }
        }
        
        # 100 transactions, $10000 volume
        derived = {"stripe": DerivedUsage("stripe", 100.0)}
        
        aggregator = CostAggregator(nodes, derived, [])
        costs = aggregator.aggregate()
        
        # Expected: $10000 * 0.029 + 100 * 0.30 = $290 + $30 = $320
        assert costs["stripe"] == pytest.approx(320.0)


class TestCostEngine:
    """Tests for full cost engine."""
    
    def test_compute_returns_costs(self):
        """Test that compute returns node costs."""
        model = make_valid_cost_model(frequency=100)
        engine = CostEngine(model)
        
        costs = engine.compute()
        
        assert isinstance(costs, dict)
        assert len(costs) > 0
    
    def test_total_cost(self):
        """Test total cost calculation."""
        model = make_valid_cost_model(frequency=100)
        engine = CostEngine(model)
        
        total = engine.total_cost()
        
        assert total >= 0
    
    def test_invalid_dag_raises(self):
        """Test that invalid DAG raises ValueError."""
        model = make_valid_cost_model()
        model["edges"].append({"from": "nonexistent", "to": "get_user_fn", "rate": 1.0})
        
        engine = CostEngine(model)
        
        with pytest.raises(ValueError, match="Invalid DAG"):
            engine.compute()