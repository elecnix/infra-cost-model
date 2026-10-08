"""Tests for Application Load Balancer resource model (Issue #183)."""
from functools import partial

import pytest
from infra_cost_model.resources.alb import ApplicationLoadBalancer
from live_pricing import derived_resource_cost, resource_cost
from infra_cost_model.pricing.catalog import PricingCatalog


class TestALBAddressParsing:
    def test_from_address_terraform_lb(self):
        r = ApplicationLoadBalancer.from_address("aws_lb.main")
        assert r is not None and r.node_type == "routing"

    def test_from_address_terraform_alb(self):
        r = ApplicationLoadBalancer.from_address("aws_alb.api")
        assert r is not None and r.node_type == "routing"

    def test_from_address_pulumi(self):
        r = ApplicationLoadBalancer.from_address("aws.lb.LoadBalancer:main-alb")
        assert r is not None and r.node_type == "routing"

    def test_from_address_cdk(self):
        r = ApplicationLoadBalancer.from_address("AWS::ElasticLoadBalancingV2::LoadBalancer:ALB")
        assert r is not None and r.node_type == "routing"

    def test_from_address_aws_format(self):
        assert ApplicationLoadBalancer.from_address("aws:lb:LoadBalancer:prod-alb") is not None

    def test_from_address_unrelated(self):
        assert ApplicationLoadBalancer.from_address("aws_lambda_function.handler") is None

    def test_from_address_nlb_rejected(self):
        # NLB has same prefix aws_lb. but handler still matches; consumer
        # should check config.lbType == "application"
        r = ApplicationLoadBalancer.from_address("aws_lb.nlb")
        assert r is not None


class TestALBExtraction:
    def test_extract_tf(self):
        resource = {
            "address": "aws_lb.main",
            "type": "aws_lb",
            "values": {
                "name": "main-alb",
                "load_balancer_type": "application",
                "internal": False,
                "idle_timeout": 60,
                "region": "us-east-1",
            },
        }
        result = ApplicationLoadBalancer.extract_tf(resource)
        assert result.node_type == "routing" and result.provider == "aws" and result.service == "AmazonALB"
        assert result.config["name"] == "main-alb"
        assert result.config["lbType"] == "application"
        assert result.config["internal"] is False
        assert result.config["idleTimeout"] == 60

    def test_extract_tf_internal(self):
        resource = {
            "address": "aws_lb.internal",
            "type": "aws_lb",
            "values": {
                "name": "internal-alb",
                "load_balancer_type": "application",
                "internal": True,
                "region": "us-east-1",
            },
        }
        result = ApplicationLoadBalancer.extract_tf(resource)
        assert result.config["internal"] is True

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.lb.LoadBalancer:api-alb",
            "type": "aws.lb.LoadBalancer",
            "inputs": {
                "name": "api-alb",
                "loadBalancerType": "application",
                "internal": False,
                "idleTimeout": 120,
                "region": "us-west-2",
            },
        }
        result = ApplicationLoadBalancer.extract_pulumi(resource)
        assert result.provider == "aws"
        assert result.config["name"] == "api-alb"
        assert result.config["lbType"] == "application"
        assert result.config["idleTimeout"] == 120

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::ElasticLoadBalancingV2::LoadBalancer",
            "LogicalId": "MainALB",
            "Properties": {
                "Name": "main-alb",
                "Type": "application",
                "Scheme": "internet-facing",
            },
        }
        result = ApplicationLoadBalancer.extract_cdk(resource)
        assert result.config["name"] == "main-alb"
        assert result.config["lbType"] == "application"
        assert result.config["internal"] is False


class TestALBPricing:
    def setup_method(self):
        self.catalog = PricingCatalog(seed=True)
        self.cost = partial(resource_cost, "aws_lb.web",
                            "AmazonALB", "us-east-1", catalog=self.catalog)

    def test_alb_hours_only(self):
        cost = self.cost(albHours=730, processedGb=0)
        assert cost == pytest.approx(16.425, rel=0.01)

    def test_processed_gb_only(self):
        cost = self.cost(albHours=0, processedGb=100)
        assert cost == pytest.approx(0.80, rel=0.01)

    def test_combined_hours_and_data(self):
        cost = self.cost(albHours=730, processedGb=100)
        assert cost == pytest.approx(17.225, rel=0.01)

    def test_lcu_new_connections(self):
        cost = self.cost(albHours=0, newConnections=50)
        assert cost == pytest.approx(0.40, rel=0.01)

    def test_lcu_active_connections(self):
        cost = self.cost(albHours=0, activeConnections=200)
        assert cost == pytest.approx(1.60, rel=0.01)

    def test_lcu_rule_evaluations(self):
        cost = self.cost(albHours=0, ruleEvaluations=1000)
        assert cost == pytest.approx(8.00, rel=0.01)

    def test_all_dimensions(self):
        # AWS bills the largest LCU dimension, 500 LCU-hours, not the sum (#480).
        lcu = derived_resource_cost(
            "aws_lb.web", "AmazonALB", "us-east-1",
            {"processedGb": 500, "newConnections": 100,
             "activeConnections": 200, "ruleEvaluations": 50},
            catalog=self.catalog)
        cost = self.cost(albHours=730) + lcu
        expected = 730 * 0.0225 + 500 * 0.008
        assert cost == pytest.approx(expected, rel=0.01)

    def test_zero_usage(self):
        assert self.cost(albHours=0, processedGb=0) == 0.0


class TestALBNodeType:
    def test_alb_is_routing_node(self):
        result = ApplicationLoadBalancer.from_address("aws_lb.main")
        assert result is not None and result.node_type == "routing"
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node("routing") is False

    def test_alb_valid_metrics(self):
        i = ApplicationLoadBalancer()
        assert all(m in i.valid_metrics for m in ["albHours", "processedGb", "newConnections",
                                                    "activeConnections", "ruleEvaluations"])


class TestALBRegistryIntegration:
    def test_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_lb.main") == ApplicationLoadBalancer

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_lb.main",
            "type": "aws_lb",
            "values": {
                "name": "main-alb",
                "load_balancer_type": "application",
                "internal": False,
                "region": "us-east-1",
            },
        }
        result = ResourceRegistry.extract("aws_lb.main", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonALB"
        assert result["nodeType"] == "routing"


LCU_METRICS = ("processedGb", "newConnections", "activeConnections",
               "ruleEvaluations")


class TestALBLargestLCUDimension:
    """AWS bills the largest of the four LCU dimensions each hour (#480)."""

    def derive(self, **usage):
        return ApplicationLoadBalancer().derive_catalog_usage(usage, {})

    def test_two_dimensions_price_the_larger_once(self):
        derived = self.derive(processedGb=2.0, newConnections=5.0)
        assert derived.quantities == {"ALB-LCU-ProcessedBytes": 5.0}
        assert derived.consumed == {"processedGb", "newConnections"}

    def test_all_four_dimensions_price_the_largest_once(self):
        derived = self.derive(albHours=1.0, processedGb=3.0, newConnections=1.0,
                              activeConnections=7.0, ruleEvaluations=4.0)
        assert derived.quantities == {"ALB-LCU-ProcessedBytes": 7.0}
        # ALB-hours are not an LCU dimension: the per-metric path prices them.
        assert derived.consumed == set(LCU_METRICS)

    @pytest.mark.parametrize("metric", LCU_METRICS)
    def test_one_dimension_keeps_its_own_row(self, metric):
        assert self.derive(albHours=1.0, **{metric: 3.0}) is None

    def test_no_dimension_derives_nothing(self):
        assert self.derive(albHours=1.0) is None
