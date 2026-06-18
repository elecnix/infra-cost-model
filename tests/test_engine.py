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
        model["workflow"]["frequency"] = {"unit": "perWeek", "value": 1}
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        with pytest.raises(ValueError, match="Unknown frequency unit 'perWeek'"):
            deriver.derive()
    
    def test_unknown_frequency_unit_lists_valid_units(self):
        """Test that error message lists valid units."""
        model = make_valid_cost_model()
        model["workflow"]["frequency"] = {"unit": "perMonth", "value": 1}
        
        deriver = WorkloadDeriver(model["workflow"], model["nodes"], model["edges"])
        
        with pytest.raises(ValueError, match="Valid units"):
            deriver.derive()
    
    def test_known_frequency_units_work(self):
        """Test that all known frequency units work without error."""
        for unit in ["perSecond", "perMinute", "perHour", "perDay"]:
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
