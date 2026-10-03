"""Tests for Secrets Manager, ECR, and Route53 resource handlers (Issue #186)."""
import pytest
from infra_cost_model.pricing.catalog import PricingCatalog
from live_pricing import resource_cost
from infra_cost_model.resources.misc_services import (
    SecretsManagerSecret, ECRRepository, Route53Zone,
)


def _sm(**usage):
    """Price a Secrets Manager secret through its declared catalog metrics."""
    return resource_cost("aws_secretsmanager_secret.s", "AWSSecretsManager",
                         "us-east-1", catalog=PricingCatalog(seed=True), **usage)


def _ecr(**usage):
    """Price an ECR repository through its declared catalog metrics."""
    return resource_cost("aws_ecr_repository.r", "AmazonECR", "us-east-1",
                         catalog=PricingCatalog(seed=True), **usage)


def _r53(**usage):
    """Price a Route 53 hosted zone through its declared catalog metrics."""
    return resource_cost("aws_route53_zone.z", "AmazonRoute53", "us-east-1",
                         catalog=PricingCatalog(seed=True), **usage)


class TestSecretsManager:
    def test_from_address_terraform(self):
        r = SecretsManagerSecret.from_address("aws_secretsmanager_secret.db_pass")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi(self):
        r = SecretsManagerSecret.from_address("aws.secretsmanager.Secret:app-secret")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        r = SecretsManagerSecret.from_address("AWS::SecretsManager::Secret:AppSecret")
        assert r is not None and r.node_type == "storage"

    def test_from_address_unrelated(self):
        assert SecretsManagerSecret.from_address("aws_lambda_function.handler") is None

    def test_extract_tf(self):
        resource = {
            "address": "aws_secretsmanager_secret.db_pass",
            "values": {
                "name": "db-password",
                "rotation": {"rotation_days": 30, "automatically_after_days": 30},
                "region": "us-east-1",
            },
        }
        result = SecretsManagerSecret.extract_tf(resource)
        assert result.node_type == "storage"
        assert result.provider == "aws"
        assert result.service == "AWSSecretsManager"
        assert result.config["name"] == "db-password"
        assert result.config["rotationDays"] == 30

    def test_extract_tf_no_rotation(self):
        resource = {
            "address": "aws_secretsmanager_secret.simple",
            "values": {"name": "api-key", "region": "us-west-2"},
        }
        result = SecretsManagerSecret.extract_tf(resource)
        assert result.config["rotationDays"] is None

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.secretsmanager.Secret:token",
            "inputs": {
                "name": "api-token",
                "rotation": {"rotationDays": 90, "automaticallyAfterDays": 90},
                "region": "us-west-2",
            },
        }
        result = SecretsManagerSecret.extract_pulumi(resource)
        assert result.config["name"] == "api-token"
        assert result.config["rotationDays"] == 90

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::SecretsManager::Secret",
            "LogicalId": "DbSecret",
            "Properties": {
                "Name": "db-secret",
                "RotationSchedule": {"RotationDays": 30},
            },
        }
        result = SecretsManagerSecret.extract_cdk(resource)
        assert result.config["name"] == "db-secret"
        assert result.config["rotationDays"] == 30

    def test_valid_metrics(self):
        sm = SecretsManagerSecret()
        assert "secretsCount" in sm.valid_metrics
        assert "apiCalls" in sm.valid_metrics

    def test_is_storage_leaf(self):
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node("storage") is True

    def test_pricing_single_secret(self):
        cost = _sm(secretsCount=1, apiCalls=0)
        assert cost == pytest.approx(0.40, rel=0.01)

    def test_pricing_multiple_secrets(self):
        cost = _sm(secretsCount=5, apiCalls=0)
        assert cost == pytest.approx(2.00, rel=0.01)

    def test_pricing_api_calls(self):
        # Pricing is per-unit; set api_calls=1 for ~$0.05
        cost = _sm(secretsCount=0, apiCalls=1)
        assert cost == pytest.approx(0.05, rel=0.01)

    def test_pricing_zero_usage(self):
        cost = _sm(secretsCount=0, apiCalls=0)
        assert cost == 0.0

    def test_registry_integration(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert (
            ResourceRegistry.from_address("aws_secretsmanager_secret.db_pass")
            == SecretsManagerSecret
        )

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_secretsmanager_secret.db_pass",
            "values": {"name": "db-password", "region": "us-east-1"},
        }
        result = ResourceRegistry.extract(
            "aws_secretsmanager_secret.db_pass", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AWSSecretsManager"


class TestECR:
    def test_from_address_terraform(self):
        r = ECRRepository.from_address("aws_ecr_repository.app")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi(self):
        r = ECRRepository.from_address("aws.ecr.Repository:app-repo")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        r = ECRRepository.from_address("AWS::ECR::Repository:AppRepo")
        assert r is not None and r.node_type == "storage"

    def test_from_address_unrelated(self):
        assert ECRRepository.from_address("aws_s3_bucket.images") is None

    def test_extract_tf(self):
        resource = {
            "address": "aws_ecr_repository.app",
            "values": {
                "name": "app-images",
                "image_tag_mutability": "IMMUTABLE",
                "region": "us-east-1",
            },
        }
        result = ECRRepository.extract_tf(resource)
        assert result.node_type == "storage"
        assert result.provider == "aws"
        assert result.service == "AmazonECR"
        assert result.config["name"] == "app-images"
        assert result.config["imageTagMutability"] == "IMMUTABLE"

    def test_extract_tf_default_mutability(self):
        resource = {
            "address": "aws_ecr_repository.default",
            "values": {"name": "default-repo", "region": "us-east-1"},
        }
        result = ECRRepository.extract_tf(resource)
        assert result.config["imageTagMutability"] == "MUTABLE"

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.ecr.Repository:shared",
            "inputs": {
                "name": "shared-images",
                "imageTagMutability": "IMMUTABLE",
                "region": "us-west-2",
            },
        }
        result = ECRRepository.extract_pulumi(resource)
        assert result.config["name"] == "shared-images"
        assert result.config["imageTagMutability"] == "IMMUTABLE"

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::ECR::Repository",
            "LogicalId": "SharedRepo",
            "Properties": {
                "RepositoryName": "shared-images",
                "ImageTagMutability": "IMMUTABLE",
            },
        }
        result = ECRRepository.extract_cdk(resource)
        assert result.config["name"] == "shared-images"
        assert result.config["imageTagMutability"] == "IMMUTABLE"

    def test_valid_metrics(self):
        ecr = ECRRepository()
        assert ecr.valid_metrics == ["storedGb"]

    def test_pricing_default_storage(self):
        cost = _ecr(storedGb=1)
        assert cost == pytest.approx(0.10, rel=0.01)

    def test_pricing_large_storage(self):
        cost = _ecr(storedGb=50)
        assert cost == pytest.approx(5.00, rel=0.01)

    def test_pricing_zero_usage(self):
        cost = _ecr(storedGb=0)
        assert cost == 0.0

    def test_registry_integration(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert (
            ResourceRegistry.from_address("aws_ecr_repository.app")
            == ECRRepository
        )

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_ecr_repository.app",
            "values": {"name": "app-images", "region": "us-east-1"},
        }
        result = ResourceRegistry.extract(
            "aws_ecr_repository.app", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AmazonECR"


class TestRoute53:
    def test_from_address_terraform(self):
        r = Route53Zone.from_address("aws_route53_zone.primary")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi(self):
        r = Route53Zone.from_address("aws.route53.Zone:main-zone")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        r = Route53Zone.from_address("AWS::Route53::HostedZone:MainZone")
        assert r is not None and r.node_type == "storage"

    def test_from_address_unrelated(self):
        assert Route53Zone.from_address("aws_lb.public") is None

    def test_extract_tf(self):
        resource = {
            "address": "aws_route53_zone.primary",
            "values": {
                "name": "example.com",
                "comment": "Primary hosted zone",
                "region": "us-east-1",
            },
        }
        result = Route53Zone.extract_tf(resource)
        assert result.node_type == "storage"
        assert result.provider == "aws"
        assert result.service == "AmazonRoute53"
        assert result.config["name"] == "example.com"
        assert result.config["comment"] == "Primary hosted zone"

    def test_extract_tf_no_comment(self):
        resource = {
            "address": "aws_route53_zone.simple",
            "values": {"name": "simple.example.com", "region": "us-west-2"},
        }
        result = Route53Zone.extract_tf(resource)
        assert result.config["comment"] is None

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.route53.Zone:public",
            "inputs": {
                "name": "public.example.com",
                "comment": "Public zone",
                "region": "us-west-2",
            },
        }
        result = Route53Zone.extract_pulumi(resource)
        assert result.config["name"] == "public.example.com"
        assert result.config["comment"] == "Public zone"

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::Route53::HostedZone",
            "LogicalId": "MainZone",
            "Properties": {
                "Name": "example.com",
                "HostedZoneConfig": {"Comment": "CDK managed zone"},
            },
        }
        result = Route53Zone.extract_cdk(resource)
        assert result.config["name"] == "example.com"
        assert result.config["comment"] == "CDK managed zone"

    def test_valid_metrics(self):
        r53 = Route53Zone()
        assert "hostedZones" in r53.valid_metrics
        assert "queries" in r53.valid_metrics

    def test_pricing_single_zone(self):
        cost = _r53(hostedZones=1, queries=0)
        assert cost == pytest.approx(0.50, rel=0.01)

    def test_pricing_multiple_zones(self):
        cost = _r53(hostedZones=3, queries=0)
        assert cost == pytest.approx(1.50, rel=0.01)

    def test_pricing_queries(self):
        cost = _r53(hostedZones=0, queries=10)
        assert cost == pytest.approx(4.00, rel=0.01)

    def test_pricing_combined(self):
        cost = _r53(hostedZones=1, queries=5)
        assert cost == pytest.approx(2.50, rel=0.01)

    def test_pricing_zero_usage(self):
        cost = _r53(hostedZones=0, queries=0)
        assert cost == 0.0

    def test_registry_integration(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert (
            ResourceRegistry.from_address("aws_route53_zone.primary")
            == Route53Zone
        )

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_route53_zone.primary",
            "values": {"name": "example.com", "region": "us-east-1"},
        }
        result = ResourceRegistry.extract(
            "aws_route53_zone.primary", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AmazonRoute53"
