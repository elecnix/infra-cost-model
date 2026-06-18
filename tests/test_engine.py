"""Tests for cost engine module."""

import pytest
from infra_cost_model.engine.engine import (
    DAGValidator, WorkloadDeriver, CostAggregator, CostEngine, DerivedUsage,
    SensitivityAnalyzer,
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
    
    def test_invocation_derivation(self):
        """Test invocation derivation through edges."""
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
    
    def test_data_size_derivation(self):
        """Test that edge dataSize is derived to child's data_in."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "compute", "resourceAddress": "compute_b"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 1.0, "type": "invoke",
             "dataSize": {"unit": "kB", "average": 50}},
        ]
        
        deriver = WorkloadDeriver(workflow, nodes, edges)
        derived = deriver.derive()
        
        # B gets 10 invocations/sec, each with 50 kB = 500 kB total
        assert derived["B"].invocation_count == 10.0
        assert derived["B"].data_in == 10.0 * 50.0  # 500 kB
    
    def test_data_size_accumulates_from_multiple_parents(self):
        """Test that data_in accumulates from multiple parent edges."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "routing", "resourceAddress": "compute_b"},
            "C": {"nodeType": "storage", "resourceAddress": "storage_c"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 0.5},
            {"from": "A", "to": "C", "rate": 0.6, "dataSize": {"unit": "kB", "average": 10}},
            {"from": "B", "to": "C", "rate": 1.0, "dataSize": {"unit": "kB", "average": 25}},
        ]
        
        deriver = WorkloadDeriver(workflow, nodes, edges)
        derived = deriver.derive()
        
        # C gets A→C data: 10 * 0.6 * 10 = 60 + B→C data: (10*0.5) * 1.0 * 25 = 125
        # B = 10 * 0.5 = 5 invocations/sec
        expected = 10 * 0.6 * 10 + 5 * 1.0 * 25  # 60 + 125 = 185
        assert derived["C"].data_in == expected

    def test_edge_type_derivation(self):
        """Test that edge types are tracked on DerivedUsage."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "storage", "resourceAddress": "storage_b"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 0.5, "type": "read"},
        ]
        
        deriver = WorkloadDeriver(workflow, nodes, edges)
        derived = deriver.derive()
        
        assert "read" in derived["B"].edge_types
    
    def test_no_data_size_when_not_specified(self):
        """Test that data_in is 0 when no dataSize on edges."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "compute", "resourceAddress": "compute_b"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 1.0},
        ]
        
        deriver = WorkloadDeriver(workflow, nodes, edges)
        derived = deriver.derive()
        
        assert derived["B"].data_in == 0.0
    
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
    
    def test_per_week_frequency(self):
        """Test perWeek frequency conversion."""
        model = make_valid_cost_model()
        model["workflow"]["frequency"] = {"unit": "perWeek", "value": 7}  # 7 per week = 1 per day
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        derived = deriver.derive()
        
        # 7 per week = 7 / 604800 = ~1.157e-05 per second
        assert derived["api_gateway"].invocation_count == pytest.approx(7.0 / 604800.0)
    
    def test_per_month_frequency(self):
        """Test perMonth frequency conversion."""
        model = make_valid_cost_model()
        model["workflow"]["frequency"] = {"unit": "perMonth", "value": 2629800}  # 1 request/sec worth per month
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        derived = deriver.derive()
        
        # 2629800 per month = 2629800 / 2629800 = 1.0 per second
        assert derived["api_gateway"].invocation_count == pytest.approx(1.0)
    
    def test_per_month_frequency_practical(self):
        """Test perMonth with practical value (3M requests/month)."""
        model = make_valid_cost_model()
        model["workflow"]["frequency"] = {"unit": "perMonth", "value": 3_000_000}
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        derived = deriver.derive()
        
        expected_per_second = 3_000_000.0 / 2629800.0
        assert derived["api_gateway"].invocation_count == pytest.approx(expected_per_second)
    
    def test_invalid_entry_node_raises(self):
        """Test that invalid entry node raises ValueError."""
        model = make_valid_cost_model(entry="nonexistent_service")
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        with pytest.raises(ValueError, match="Entry node 'nonexistent_service' not found in nodes"):
            deriver.derive()
    
    def test_invalid_entry_node_lists_available_nodes(self):
        """Test that error message includes available node names."""
        model = make_valid_cost_model(entry="typo_api_gateway")
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        with pytest.raises(ValueError, match="Available nodes"):
            deriver.derive()
    
    def test_unknown_frequency_unit_raises(self):
        """Test that unknown frequency unit raises ValueError."""
        model = make_valid_cost_model()
        model["workflow"]["frequency"] = {"unit": "perYear", "value": 1}
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        with pytest.raises(ValueError, match="Unknown frequency unit 'perYear'"):
            deriver.derive()
    
    def test_unknown_frequency_unit_lists_valid_units(self):
        """Test that error message lists valid units."""
        model = make_valid_cost_model()
        model["workflow"]["frequency"] = {"unit": "perDecade", "value": 1}
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        with pytest.raises(ValueError, match="Valid units"):
            deriver.derive()
    
    def test_known_frequency_units_work(self):
        """Test that all known frequency units work without error."""
        for unit in ["perSecond", "perMinute", "perHour", "perDay", "perWeek", "perMonth"]:
            model = make_valid_cost_model()
            model["workflow"]["frequency"] = {"unit": unit, "value": 10}
            deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
            derived = deriver.derive()
            assert "api_gateway" in derived
    
    def test_unreachable_nodes_warning(self):
        """Test that unreachable nodes emit a warning."""
        model = make_valid_cost_model()
        # Add a disconnected node with no edges pointing to it
        model["nodes"]["orphaned_cache"] = {
            "nodeType": "storage",
            "resourceAddress": "aws_elasticache_cluster.cache",
            "provider": "aws",
            "service": "AmazonElastiCache",
        }
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        with pytest.warns(UserWarning, match="orphaned_cache"):
            deriver.derive()
    
    def test_unreachable_nodes_not_in_derived_usage(self):
        """Test that unreachable nodes are not included in derived usage."""
        model = make_valid_cost_model()
        model["nodes"]["orphaned_cache"] = {
            "nodeType": "storage",
            "resourceAddress": "aws_elasticache_cluster.cache",
            "provider": "aws",
            "service": "AmazonElastiCache",
        }
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        derived = deriver.derive()
        
        assert "orphaned_cache" not in derived
        assert "api_gateway" in derived  # reachable nodes still derived
    
    def test_no_warning_when_all_nodes_reachable(self):
        """Test that no warning is emitted when all nodes are reachable."""
        model = make_valid_cost_model()
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        import warnings
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            deriver.derive()
        
        # Filter out any unrelated warnings
        unreachable_warnings = [w for w in record if "unreachable" in str(w.message).lower()]
        assert len(unreachable_warnings) == 0
    
    def test_multiple_unreachable_nodes(self):
        """Test warning lists all unreachable node names."""
        model = make_valid_cost_model()
        model["nodes"]["orphaned_a"] = {
            "nodeType": "compute",
            "resourceAddress": "orphan_a",
            "provider": "aws",
            "service": "AWSLambda",
        }
        model["nodes"]["orphaned_b"] = {
            "nodeType": "storage",
            "resourceAddress": "orphan_b",
            "provider": "aws",
            "service": "AmazonS3",
        }
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        with pytest.warns(UserWarning, match="orphaned_a") as w:
            deriver.derive()
        
        # Warning should mention both orphaned nodes
        warning_msg = str(w[0].message)
        assert "orphaned_a" in warning_msg
        assert "orphaned_b" in warning_msg


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
    
    def test_flat_pricing_prefers_catalog(self):
        """Test flat pricing uses catalog when available (Principle 13)."""
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile
        
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            
            # Catalog has price $0.15/million (different from embedded $0.20)
            cache.upsert(Price(
                vendor="aws", service="AWSLambda", region="us-east-1",
                product_family="Serverless", attributes={},
                usage_metric="invocations", unit="requests",
                price_usd=0.15e-6,
                start_usage_amount=0, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            
            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")
            
            nodes = {
                "test_fn": {
                    "nodeType": "compute",
                    "resourceAddress": "aws_lambda_function.test",
                    "provider": "aws",
                    "service": "AWSLambda",
                    "region": "us-east-1",
                    "usageMetrics": {
                        "invocations": {"unit": "requests", "value": 1000},
                    },
                    "pricingRates": {
                        "invocations": 0.20e-6,
                    }
                }
            }
            derived = {"test_fn": DerivedUsage("test_fn", 1000.0)}
            
            aggregator = CostAggregator(nodes, derived, [], catalog)
            costs = aggregator.aggregate()
            
            # 1000 invocations × 1000 per-invocation × $0.15e-6 = $0.15
            # Should use catalog price $0.15, not embedded $0.20
            assert costs["test_fn"] == pytest.approx(1000 * 1000 * 0.15e-6)
    

    def test_flat_override_uses_direct_values(self):
        """Test that flatOverride=true uses values as flat monthly totals."""
        nodes = {
            "test_fn": {
                "nodeType": "compute",
                "resourceAddress": "test_fn",
                "flatOverride": True,
                "usageMetrics": {
                    "requests": {"unit": "requests", "value": 1000000},
                },
                "pricingRates": {
                    "requests": 0.20e-6,  # $0.20 per million
                }
            }
        }
        
        # Even with 1000 invocations, flatOverride uses direct value
        derived = {"test_fn": DerivedUsage("test_fn", 1000.0)}
        aggregator = CostAggregator(nodes, derived, [])
        costs = aggregator.aggregate()
        
        # 1,000,000 requests × $0.20e-6 = $0.20 (flat, NOT 1000 × 1M × rate)
        expected = 1000000 * 0.20e-6
        assert costs["test_fn"] == pytest.approx(expected)

    def test_flat_override_false_default(self):
        """Test that without flatOverride, behavior is unchanged."""
        nodes = {
            "test_fn": {
                "nodeType": "compute",
                "resourceAddress": "test_fn",
                "usageMetrics": {
                    "requests": {"unit": "requests", "value": 1},
                },
                "pricingRates": {
                    "requests": 0.20e-6,
                }
            }
        }
        
        # 1000 invocations × 1 request each × rate
        derived = {"test_fn": DerivedUsage("test_fn", 1000.0)}
        aggregator = CostAggregator(nodes, derived, [])
        costs = aggregator.aggregate()
        
        expected = 1000 * 1 * 0.20e-6
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
    
    def test_tiered_pricing_with_catalog(self):
        """Test tiered pricing uses catalog when available."""
        from infra_cost_model.pricing.cache import PricingCache, Price, TieredPrice
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile
        
        # Create a catalog with tiered S3 storage pricing
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            
            # Tier 1: first 50 TB at $0.023/GB
            cache.upsert(Price(
                vendor="aws", service="AmazonS3", region="us-east-1",
                product_family="Storage", attributes={},
                usage_metric="storageGb", unit="GB-Mo",
                price_usd=0.023,
                start_usage_amount=0, end_usage_amount=50_000,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            # Tier 2: next 450 TB at $0.022/GB
            cache.upsert(Price(
                vendor="aws", service="AmazonS3", region="us-east-1",
                product_family="Storage", attributes={},
                usage_metric="storageGb", unit="GB-Mo",
                price_usd=0.022,
                start_usage_amount=50_000, end_usage_amount=500_000,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            # Tier 3: over 500 TB at $0.021/GB
            cache.upsert(Price(
                vendor="aws", service="AmazonS3", region="us-east-1",
                product_family="Storage", attributes={},
                usage_metric="storageGb", unit="GB-Mo",
                price_usd=0.021,
                start_usage_amount=500_000, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            
            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")
            
            nodes = {
                "s3_bucket": {
                    "nodeType": "storage",
                    "resourceAddress": "aws_s3_bucket.data",
                    "provider": "aws",
                    "service": "AmazonS3",
                    "region": "us-east-1",
                    "pricingModel": "tiered",
                    "usageMetrics": {
                        "storageGb": {"unit": "GB-Mo", "value": 1000},
                    },
                }
            }
            
            # 1000 GB storage, 1 invocation
            derived = {"s3_bucket": DerivedUsage("s3_bucket", 1.0)}
            aggregator = CostAggregator(nodes, derived, [], catalog)
            costs = aggregator.aggregate()
            
            # 1000 GB should all be in first tier: 1000 * $0.023 = $23.00
            assert costs["s3_bucket"] == pytest.approx(1000 * 0.023)
    
    def test_tiered_pricing_crosses_tiers(self):
        """Test tiered pricing correctly handles crossing tier boundaries."""
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile
        
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            
            # Tier 1: first 10 units at $1.00
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="units", unit="units",
                price_usd=1.00,
                start_usage_amount=0, end_usage_amount=10,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            # Tier 2: next 90 units at $0.50
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="units", unit="units",
                price_usd=0.50,
                start_usage_amount=10, end_usage_amount=100,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            # Tier 3: above 100 at $0.25
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="units", unit="units",
                price_usd=0.25,
                start_usage_amount=100, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            
            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")
            
            nodes = {
                "svc": {
                    "nodeType": "compute",
                    "resourceAddress": "test.svc",
                    "provider": "aws",
                    "service": "TestSvc",
                    "region": "us-east-1",
                    "pricingModel": "tiered",
                    "usageMetrics": {
                        "units": {"unit": "units", "value": 25},
                    },
                }
            }
            
            # 25 units: 10 * $1.00 + 15 * $0.50 = $10 + $7.50 = $17.50
            derived = {"svc": DerivedUsage("svc", 1.0)}
            aggregator = CostAggregator(nodes, derived, [], catalog)
            costs = aggregator.aggregate()
            
            assert costs["svc"] == pytest.approx(17.50)
    
    def test_tiered_pricing_fallback_to_flat(self):
        """Test tiered pricing falls back to flat pricingRates when no catalog."""
        nodes = {
            "s3_bucket": {
                "nodeType": "storage",
                "resourceAddress": "aws_s3_bucket.data",
                "provider": "aws",
                "service": "AmazonS3",
                "pricingModel": "tiered",
                "usageMetrics": {
                    "storageGb": {"unit": "GB-Mo", "value": 1000},
                },
                "pricingRates": {
                    "storageGb": 0.023,
                }
            }
        }
        
        derived = {"s3_bucket": DerivedUsage("s3_bucket", 1.0)}
        aggregator = CostAggregator(nodes, derived, [], catalog=None)
        costs = aggregator.aggregate()
        
        # Falls back to flat: 1000 * $0.023 = $23.00
        assert costs["s3_bucket"] == pytest.approx(23.0)
    
    def test_tiered_pricing_with_free_tier(self):
        """Test free-tier pricing (first N units at $0)."""
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile
        
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            
            # Free tier: first 5 units at $0
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="requests", unit="requests",
                price_usd=0.0,
                start_usage_amount=0, end_usage_amount=5,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            # Paid tier: above 5 at $0.10
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="requests", unit="requests",
                price_usd=0.10,
                start_usage_amount=5, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            
            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")
            
            nodes = {
                "svc": {
                    "nodeType": "compute",
                    "resourceAddress": "test.svc",
                    "provider": "aws",
                    "service": "TestSvc",
                    "region": "us-east-1",
                    "pricingModel": "tiered",
                    "usageMetrics": {
                        "requests": {"unit": "requests", "value": 3},
                    },
                }
            }
            
            # 3 requests, all in free tier: $0
            derived = {"svc": DerivedUsage("svc", 1.0)}
            aggregator = CostAggregator(nodes, derived, [], catalog)
            costs = aggregator.aggregate()
            
            assert costs["svc"] == 0.0
            
            # Now with 8 requests: 5 free + 3 * $0.10 = $0.30
            nodes["svc"]["usageMetrics"]["requests"]["value"] = 8
            derived2 = {"svc": DerivedUsage("svc", 1.0)}
            aggregator2 = CostAggregator(nodes, derived2, [], catalog)
            costs2 = aggregator2.aggregate()
            
            assert costs2["svc"] == pytest.approx(0.30)
    
    def test_tiered_pricing_multiple_metrics(self):
        """Test tiered pricing with multiple metrics per node."""
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile
        
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            
            # Storage metric
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="storageGb", unit="GB-Mo",
                price_usd=0.023,
                start_usage_amount=0, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            # Request metric
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="requests", unit="requests",
                price_usd=0.005,
                start_usage_amount=0, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            
            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")
            
            nodes = {
                "svc": {
                    "nodeType": "compute",
                    "resourceAddress": "test.svc",
                    "provider": "aws",
                    "service": "TestSvc",
                    "region": "us-east-1",
                    "pricingModel": "tiered",
                    "usageMetrics": {
                        "storageGb": {"unit": "GB-Mo", "value": 100},
                        "requests": {"unit": "requests", "value": 1000},
                    },
                }
            }
            
            # 100 GB storage * $0.023 + 1000 requests * $0.005 = $2.30 + $5.00
            derived = {"svc": DerivedUsage("svc", 1.0)}
            aggregator = CostAggregator(nodes, derived, [], catalog)
            costs = aggregator.aggregate()
            
            assert costs["svc"] == pytest.approx(2.30 + 5.00)


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
    
    def test_monthly_time_basis(self):
        """Test that monthly time_basis converts per-second costs to monthly."""
        from infra_cost_model.engine.engine import SECONDS_PER_MONTH
        
        model = make_valid_cost_model(frequency=100)
        
        # Per-second engine
        per_second = CostEngine(model, time_basis="perSecond")
        ps_costs = per_second.compute()
        ps_total = per_second.total_cost()
        
        # Monthly engine
        monthly = CostEngine(model, time_basis="monthly")
        mo_costs = monthly.compute()
        mo_total = monthly.total_cost()
        
        # Monthly should be SECONDS_PER_MONTH × per-second
        assert mo_total == pytest.approx(ps_total * SECONDS_PER_MONTH)
        for addr in ps_costs:
            assert mo_costs[addr] == pytest.approx(ps_costs[addr] * SECONDS_PER_MONTH)
    
    def test_invalid_entry_node_through_engine(self):
        """Test that invalid entry node raises ValueError through full engine."""
        model = make_valid_cost_model(entry="mistyped_entry")
        engine = CostEngine(model)
        
        with pytest.raises(ValueError, match="Entry node 'mistyped_entry' not found"):
            engine.compute()


class TestProviderRegionValidation:
    """Tests for DP#6: provider/region must be explicit, no AWS defaults."""

    def test_missing_provider_with_catalog_raises(self):
        """Node with usageMetrics and catalog but no provider raises ValueError."""
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="requests", unit="requests",
                price_usd=0.001,
                start_usage_amount=0, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")

            nodes = {
                "svc": {
                    "nodeType": "compute",
                    "resourceAddress": "test.svc",
                    # provider intentionally missing
                    "service": "TestSvc",
                    "region": "us-east-1",
                    "usageMetrics": {
                        "requests": {"unit": "requests", "value": 100},
                    },
                }
            }
            derived = {"svc": DerivedUsage("svc", 1.0)}
            aggregator = CostAggregator(nodes, derived, [], catalog)

            with pytest.raises(ValueError, match="missing required 'provider'"):
                aggregator.aggregate()

    def test_missing_region_with_catalog_raises(self):
        """Node with usageMetrics and catalog but no region raises ValueError."""
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="requests", unit="requests",
                price_usd=0.001,
                start_usage_amount=0, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")

            nodes = {
                "svc": {
                    "nodeType": "compute",
                    "resourceAddress": "test.svc",
                    "provider": "gcp",
                    "service": "TestSvc",
                    # region intentionally missing
                    "usageMetrics": {
                        "requests": {"unit": "requests", "value": 100},
                    },
                }
            }
            derived = {"svc": DerivedUsage("svc", 1.0)}
            aggregator = CostAggregator(nodes, derived, [], catalog)

            with pytest.raises(ValueError, match="missing required 'region'"):
                aggregator.aggregate()

    def test_missing_provider_without_catalog_ok(self):
        """Without catalog, missing provider is fine (uses embedded pricingRates)."""
        nodes = {
            "svc": {
                "nodeType": "compute",
                "resourceAddress": "test.svc",
                "usageMetrics": {
                    "requests": {"unit": "requests", "value": 100},
                },
                "pricingRates": {
                    "requests": 0.001,
                }
            }
        }
        derived = {"svc": DerivedUsage("svc", 1.0)}
        aggregator = CostAggregator(nodes, derived, [], catalog=None)
        costs = aggregator.aggregate()
        # Should compute cost using embedded pricingRates
        assert costs["svc"] == pytest.approx(100 * 0.001)

    def test_missing_provider_no_usage_metrics_ok(self):
        """Nodes without usageMetrics don't need provider (e.g., routing nodes)."""
        nodes = {
            "router": {
                "nodeType": "routing",
                "resourceAddress": "route",
            }
        }
        derived = {"router": DerivedUsage("router", 100.0)}
        aggregator = CostAggregator(nodes, derived, [], catalog=None)
        costs = aggregator.aggregate()
        # Routing node with no usageMetrics should have zero cost
        assert "router" in costs

    def test_tiered_missing_provider_with_catalog_raises(self):
        """Tiered pricing with catalog but no provider raises ValueError."""
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            cache.upsert(Price(
                vendor="aws", service="TestSvc", region="us-east-1",
                product_family="Test", attributes={},
                usage_metric="storage", unit="GB",
                price_usd=0.023,
                start_usage_amount=0, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))
            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")

            nodes = {
                "svc": {
                    "nodeType": "storage",
                    "resourceAddress": "test.svc",
                    # provider intentionally missing
                    "service": "TestSvc",
                    "region": "us-east-1",
                    "pricingModel": "tiered",
                    "usageMetrics": {
                        "storage": {"unit": "GB", "value": 1000},
                    },
                }
            }
            derived = {"svc": DerivedUsage("svc", 1.0)}
            aggregator = CostAggregator(nodes, derived, [], catalog)

            with pytest.raises(ValueError, match="missing required 'provider'"):
                aggregator.aggregate()


class TestParameterIntegration:
    """Tests for DP#4: symbolic parameters in workload derivation and pricing."""

    def test_parameter_resolution_in_edge_rates(self):
        """Edge rates can reference parameters by name."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
            "parameters": {"cache_hit_rate": 0.8},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "compute", "resourceAddress": "compute_b"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": "cache_hit_rate"},
        ]

        deriver = WorkloadDeriver(workflow, nodes, edges,
                                   parameters=workflow["parameters"])
        derived = deriver.derive()

        # B gets 10.0 * 0.8 = 8.0 invocations/sec
        assert derived["B"].invocation_count == 8.0

    def test_parameter_resolution_in_usage_metrics(self):
        """Usage metric values can reference parameters by name."""
        nodes = {
            "test_fn": {
                "nodeType": "compute",
                "resourceAddress": "test_fn",
                "provider": "aws",
                "service": "AWSLambda",
                "usageMetrics": {
                    "invocations": {"unit": "requests", "value": "traffic_multiplier"},
                },
                "pricingRates": {
                    "invocations": 0.001,
                }
            }
        }
        derived = {"test_fn": DerivedUsage("test_fn", 100.0)}
        parameters = {"traffic_multiplier": 5.0}
        aggregator = CostAggregator(nodes, derived, [], parameters=parameters)
        costs = aggregator.aggregate()

        # 100 invocations * 5 (traffic_multiplier) * $0.001 = $0.50
        assert costs["test_fn"] == pytest.approx(100 * 5 * 0.001)

    def test_what_if_with_symbolic_parameter(self):
        """SensitivityAnalyzer.what_if supports symbolic parameter names."""
        model = {
            "version": "1.0",
            "workflow": {
                "name": "test",
                "entry": "A",
                "frequency": {"unit": "perSecond", "value": 10.0},
                "parameters": {"cache_hit_rate": 0.5},
            },
            "nodes": {
                "A": {"nodeType": "routing", "resourceAddress": "entry"},
                "B": {
                    "nodeType": "compute",
                    "resourceAddress": "compute_b",
                    "provider": "aws",
                    "service": "AWSLambda",
                    "usageMetrics": {
                        "invocations": {"unit": "requests", "value": 1},
                    },
                    "pricingRates": {"invocations": 0.001},
                },
            },
            "edges": [
                {"from": "A", "to": "B", "rate": "cache_hit_rate"},
            ],
        }

        analyzer = SensitivityAnalyzer(model)

        # With cache_hit_rate=0.5: B gets 10 * 0.5 = 5 invocations * $0.001 = $0.005
        cost_05 = analyzer.what_if("cache_hit_rate", 0.5)
        assert cost_05 == pytest.approx(5 * 0.001)

        # With cache_hit_rate=0.9: B gets 10 * 0.9 = 9 invocations * $0.001 = $0.009
        cost_09 = analyzer.what_if("cache_hit_rate", 0.9)
        assert cost_09 == pytest.approx(9 * 0.001)

    def test_sensitivity_with_symbolic_parameter(self):
        """Sensitivity analysis works with symbolic parameters."""
        model = {
            "version": "1.0",
            "workflow": {
                "name": "test",
                "entry": "A",
                "frequency": {"unit": "perSecond", "value": 10.0},
                "parameters": {"cache_hit_rate": 0.5},
            },
            "nodes": {
                "A": {"nodeType": "routing", "resourceAddress": "entry"},
                "B": {
                    "nodeType": "compute",
                    "resourceAddress": "compute_b",
                    "provider": "aws",
                    "service": "AWSLambda",
                    "usageMetrics": {
                        "invocations": {"unit": "requests", "value": 1},
                    },
                    "pricingRates": {"invocations": 0.001},
                },
            },
            "edges": [
                {"from": "A", "to": "B", "rate": "cache_hit_rate"},
            ],
        }

        analyzer = SensitivityAnalyzer(model)
        results = analyzer.sensitivity("cache_hit_rate", steps=3)

        assert len(results) == 3
        # Values should range from 0.5*0.5=0.25 to 0.5*2.0=1.0
        assert results[0][0] == pytest.approx(0.25)  # 0.5×0.5
        assert results[2][0] == pytest.approx(1.0)   # 0.5×2.0

    def test_unrecognized_parameter_reference_raises_in_deriver(self):
        """An unrecognized parameter name in edge rate raises ValueError."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "compute", "resourceAddress": "compute_b"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": "nonexistent_param"},
        ]

        deriver = WorkloadDeriver(workflow, nodes, edges)
        with pytest.raises(ValueError, match="Unrecognized parameter reference"):
            deriver.derive()

    def test_unrecognized_parameter_reference_raises_in_aggregator(self):
        """An unrecognized parameter name in usage metric raises ValueError."""
        nodes = {
            "test_fn": {
                "nodeType": "compute",
                "resourceAddress": "test_fn",
                "usageMetrics": {
                    "invocations": {"unit": "requests", "value": "nonexistent_param"},
                },
                "pricingRates": {"invocations": 0.001},
            }
        }
        derived = {"test_fn": DerivedUsage("test_fn", 100.0)}
        aggregator = CostAggregator(nodes, derived, [])

        with pytest.raises(ValueError, match="Unrecognized parameter reference"):
            aggregator.aggregate()

    def test_numeric_edge_rate_still_works_with_parameters(self):
        """Numeric edge rates work normally even when parameters are defined."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
            "parameters": {"unused_param": 0.5},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "compute", "resourceAddress": "compute_b"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 0.75},
        ]

        deriver = WorkloadDeriver(workflow, nodes, edges,
                                   parameters=workflow["parameters"])
        derived = deriver.derive()

        assert derived["B"].invocation_count == 7.5

    def test_parameter_impact_with_symbolic_parameter(self):
        """SensitivityAnalyzer.parameter_impact works with symbolic parameters."""
        model = {
            "version": "1.0",
            "workflow": {
                "name": "test",
                "entry": "A",
                "frequency": {"unit": "perSecond", "value": 10.0},
                "parameters": {"cache_hit_rate": 0.5},
            },
            "nodes": {
                "A": {"nodeType": "routing", "resourceAddress": "entry"},
                "B": {
                    "nodeType": "compute",
                    "resourceAddress": "compute_b",
                    "provider": "aws",
                    "service": "AWSLambda",
                    "usageMetrics": {
                        "invocations": {"unit": "requests", "value": 1},
                    },
                    "pricingRates": {"invocations": 0.001},
                },
            },
            "edges": [
                {"from": "A", "to": "B", "rate": "cache_hit_rate"},
            ],
        }

        analyzer = SensitivityAnalyzer(model)
        impact = analyzer.parameter_impact("cache_hit_rate", delta=0.1)

        # +10% change: 0.5*1.1=0.55, B gets 10*0.55=5.5, cost=5.5*0.001=0.0055
        # baseline: 0.5, B gets 10*0.5=5, cost=5*0.001=0.005
        # impact = 0.0055 - 0.005 = 0.0005
        assert impact == pytest.approx(0.0005)


class TestTokenDistribution:
    """Tests for DP#8: token flow distribution through DAG."""

    def test_token_flow_distribution(self):
        """Token flow from edges accumulates on child as input_tokens."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "compute", "resourceAddress": "llm"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 1.0,
             "tokenFlow": {"input": 1000}},
        ]

        deriver = WorkloadDeriver(workflow, nodes, edges)
        derived = deriver.derive()

        # B gets 10 invocations/sec, each with 1000 input tokens = 10000
        assert derived["B"].invocation_count == 10.0
        assert derived["B"].input_tokens == 10000.0

    def test_token_flow_accumulates_from_multiple_parents(self):
        """Input tokens accumulate from multiple parent edges."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "routing", "resourceAddress": "router_b"},
            "C": {"nodeType": "compute", "resourceAddress": "llm_c"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 0.5},
            {"from": "A", "to": "C", "rate": 0.6,
             "tokenFlow": {"input": 1000}},
            {"from": "B", "to": "C", "rate": 1.0,
             "tokenFlow": {"input": 500}},
        ]

        deriver = WorkloadDeriver(workflow, nodes, edges)
        derived = deriver.derive()

        # C gets: A→C: 10*0.6*1000=6000 + B→C: (10*0.5)*1.0*500=2500 = 8500
        expected = 10 * 0.6 * 1000 + (10 * 0.5) * 1.0 * 500  # 6000 + 2500
        assert derived["C"].input_tokens == expected

    def test_no_token_flow_when_not_specified(self):
        """input_tokens is 0 when no tokenFlow on edges."""
        workflow = {
            "entry": "A",
            "frequency": {"unit": "perSecond", "value": 10.0},
        }
        nodes = {
            "A": {"nodeType": "routing", "resourceAddress": "entry"},
            "B": {"nodeType": "compute", "resourceAddress": "compute_b"},
        }
        edges = [
            {"from": "A", "to": "B", "rate": 1.0},
        ]

        deriver = WorkloadDeriver(workflow, nodes, edges)
        derived = deriver.derive()

        assert derived["B"].input_tokens == 0.0
        assert derived["B"].output_tokens == 0.0

    def test_token_based_pricing_with_pricing_rates(self):
        """Token-based pricing uses input/output token pricing rates."""
        nodes = {
            "llm": {
                "nodeType": "compute",
                "resourceAddress": "bedrock.claude",
                "provider": "aws",
                "service": "Bedrock",
                "pricingModel": "token_based",
                "usageMetrics": {
                    "inputTokens": {"unit": "tokens", "value": 1000},
                    "outputTokens": {"unit": "tokens", "value": 500},
                },
                "pricingRates": {
                    "inputTokens": 0.003 / 1000,   # $0.003 per 1K input tokens
                    "outputTokens": 0.015 / 1000,  # $0.015 per 1K output tokens
                }
            }
        }

        # 100 invocations, 1000 input tokens each, 500 output tokens each
        usage = DerivedUsage("llm", invocation_count=100.0,
                             input_tokens=100000.0,  # 100 * 1000
                             output_tokens=50000.0)  # will be recomputed

        aggregator = CostAggregator(nodes, {"llm": usage}, [])
        costs = aggregator.aggregate()

        # Input: 100000 * $0.003/1K = $0.30
        # Output: 100 * 500 * $0.015/1K = 50000 * $0.015/1K = $0.75
        # Total: $1.05
        input_cost = 100000 * 0.003 / 1000
        output_cost = 50000 * 0.015 / 1000
        assert costs["llm"] == pytest.approx(input_cost + output_cost)

    def test_token_based_pricing_with_catalog(self):
        """Token-based pricing uses catalog when available (Principle 13)."""
        from infra_cost_model.pricing.cache import PricingCache, Price
        from infra_cost_model.pricing.catalog import PricingCatalog
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = PricingCache(db_path=Path(tmpdir) / "test.db")
            cache.upsert(Price(
                vendor="aws", service="Bedrock", region="us-east-1",
                product_family="Bedrock", attributes={},
                usage_metric="outputTokens", unit="tokens",
                price_usd=0.015 / 1000,
                start_usage_amount=0, end_usage_amount=None,
                source="test", effective_date="2024-01-01",
                fetched_at="2024-01-01T00:00:00"
            ))

            catalog = PricingCatalog(db_path=Path(tmpdir) / "test.db")

            nodes = {
                "llm": {
                    "nodeType": "compute",
                    "resourceAddress": "bedrock.claude",
                    "provider": "aws",
                    "service": "Bedrock",
                    "region": "us-east-1",
                    "pricingModel": "token_based",
                    "usageMetrics": {
                        "outputTokens": {"unit": "tokens", "value": 500},
                    },
                    "pricingRates": {
                        "outputTokens": 0.001 / 1000,  # Will be overridden by catalog
                    }
                }
            }

            usage = DerivedUsage("llm", invocation_count=100.0)
            aggregator = CostAggregator(nodes, {"llm": usage}, [], catalog)
            costs = aggregator.aggregate()

            # 100 * 500 * $0.015/1K from catalog = $0.75
            expected = 100 * 500 * 0.015 / 1000
            assert costs["llm"] == pytest.approx(expected)

    def test_token_flow_with_token_based_pricing(self):
        """End-to-end: token flow distribution → token-based pricing."""
        model = {
            "version": "1.0",
            "workflow": {
                "name": "llm-workflow",
                "entry": "api",
                "frequency": {"unit": "perSecond", "value": 10.0},
            },
            "nodes": {
                "api": {"nodeType": "routing", "resourceAddress": "entry"},
                "llm": {
                    "nodeType": "compute",
                    "resourceAddress": "bedrock.claude",
                    "provider": "aws",
                    "service": "Bedrock",
                    "pricingModel": "token_based",
                    "usageMetrics": {
                        "outputTokens": {"unit": "tokens", "value": 500},
                    },
                    "pricingRates": {
                        "inputTokens": 0.003 / 1000,
                        "outputTokens": 0.015 / 1000,
                    },
                },
            },
            "edges": [
                {"from": "api", "to": "llm", "rate": 1.0,
                 "tokenFlow": {"input": 1000}},
            ],
        }

        engine = CostEngine(model)
        costs = engine.compute()

        # 10 invocations/sec, 1000 input tokens each
        # Input cost: 10*1000 * $0.003/1K = $0.03 per second
        # Output: 10*500 * $0.015/1K = $0.075 per second
        # Total: $0.105 per second
        expected = 10 * 1000 * 0.003 / 1000 + 10 * 500 * 0.015 / 1000
        assert costs["llm"] == pytest.approx(expected)

        # Also verify token flow tracking
        usage = engine.get_derived_usage()
        assert usage["llm"].input_tokens == 10000.0  # 10 * 1000

    def test_token_output_from_node_usage_metrics(self):
        """Output tokens are computed from node usageMetrics.outputTokens."""
        nodes = {
            "llm": {
                "nodeType": "compute",
                "resourceAddress": "bedrock.claude",
                "provider": "aws",
                "service": "Bedrock",
                "pricingModel": "token_based",
                "usageMetrics": {
                    "outputTokens": {"unit": "tokens", "value": 750},
                },
                "pricingRates": {
                    "outputTokens": 0.015 / 1000,
                },
            }
        }

        # 200 invocations, no input tokens from edges, output from metrics
        usage = DerivedUsage("llm", invocation_count=200.0)
        aggregator = CostAggregator(nodes, {"llm": usage}, [])
        costs = aggregator.aggregate()

        # 200 * 750 * $0.015/1K = $2.25
        expected = 200 * 750 * 0.015 / 1000
        assert costs["llm"] == pytest.approx(expected)
