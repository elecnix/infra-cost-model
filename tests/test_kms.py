"""Tests for the AWS KMS key resource handler (Issue #208)."""
import pytest
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.resources.kms import KMSKey, _kms_cost


class TestKMSAddress:
    def test_from_address_terraform(self):
        r = KMSKey.from_address("aws_kms_key.main")
        assert r is not None and r.node_type == "storage"

    def test_from_address_pulumi(self):
        r = KMSKey.from_address("aws.kms.Key:app-key")
        assert r is not None and r.node_type == "storage"

    def test_from_address_cdk(self):
        # CDK synthetic address format: "<Type>:<LogicalId>"
        r = KMSKey.from_address("AWS::KMS::Key:AppKey")
        assert r is not None and r.node_type == "storage"

    def test_from_address_unrelated(self):
        assert KMSKey.from_address("aws_lambda_function.handler") is None
        assert KMSKey.from_address("aws_s3_bucket.data") is None
        # AWS::KMS::ReplicaKey ends with "Key" but is a distinct type; must not match.
        assert KMSKey.from_address("AWS::KMS::ReplicaKey:MyReplica") is None


class TestKMSExtract:
    def test_extract_tf(self):
        resource = {
            "address": "aws_kms_key.main",
            "values": {
                "description": "primary encryption key",
                "key_usage": "ENCRYPT_DECRYPT",
                "region": "us-east-1",
            },
        }
        result = KMSKey.extract_tf(resource)
        assert result.node_type == "storage"
        assert result.provider == "aws"
        assert result.service == "AWSKMS"
        assert result.region == "us-east-1"
        assert result.config["description"] == "primary encryption key"
        assert result.config["keyUsage"] == "ENCRYPT_DECRYPT"

    def test_extract_tf_defaults(self):
        resource = {
            "address": "aws_kms_key.simple",
            "values": {"region": "us-west-2"},
        }
        result = KMSKey.extract_tf(resource)
        assert result.config["description"] is None
        assert result.config["keyUsage"] == "ENCRYPT_DECRYPT"

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.kms.Key:token",
            "inputs": {
                "description": "token key",
                "keyUsage": "SIGN_VERIFY",
                "region": "us-west-2",
            },
        }
        result = KMSKey.extract_pulumi(resource)
        assert result.service == "AWSKMS"
        assert result.config["description"] == "token key"
        assert result.config["keyUsage"] == "SIGN_VERIFY"

    def test_extract_pulumi_defaults(self):
        resource = {"id": "aws.kms.Key:plain", "inputs": {}}
        result = KMSKey.extract_pulumi(resource)
        assert result.config["description"] is None
        assert result.config["keyUsage"] == "ENCRYPT_DECRYPT"

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::KMS::Key",
            "LogicalId": "AppKey",
            "Properties": {
                "Description": "cdk key",
                "KeyUsage": "ENCRYPT_DECRYPT",
            },
        }
        result = KMSKey.extract_cdk(resource)
        assert result.service == "AWSKMS"
        assert result.config["description"] == "cdk key"
        assert result.config["keyUsage"] == "ENCRYPT_DECRYPT"

    def test_extract_cdk_defaults(self):
        resource = {
            "Type": "AWS::KMS::Key",
            "LogicalId": "PlainKey",
            "Properties": {},
        }
        result = KMSKey.extract_cdk(resource)
        assert result.config["description"] is None
        assert result.config["keyUsage"] == "ENCRYPT_DECRYPT"


class TestKMSNodeAndMetrics:
    def test_node_type(self):
        assert KMSKey().node_type == "storage"

    def test_valid_metrics(self):
        kms = KMSKey()
        assert "keysCount" in kms.valid_metrics
        assert "apiRequests" in kms.valid_metrics

    def test_is_storage_leaf(self):
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node("storage") is True


class TestKMSPricing:
    def test_pricing_single_key(self):
        catalog = PricingCatalog(seed=True)
        cost = _kms_cost(keys_count=1, api_requests=0,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(1.00, rel=0.01)

    def test_pricing_multiple_keys(self):
        catalog = PricingCatalog(seed=True)
        cost = _kms_cost(keys_count=4, api_requests=0,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(4.00, rel=0.01)

    def test_pricing_api_requests_within_free_tier(self):
        catalog = PricingCatalog(seed=True)
        # 20,000 requests are free
        cost = _kms_cost(keys_count=0, api_requests=20000,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(0.0, abs=1e-9)

    def test_pricing_api_requests_above_free_tier(self):
        catalog = PricingCatalog(seed=True)
        # 30,000 requests -> 10,000 billable at $0.000003 = $0.03
        cost = _kms_cost(keys_count=0, api_requests=30000,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(0.03, rel=0.01)

    def test_pricing_combined(self):
        catalog = PricingCatalog(seed=True)
        # 2 keys ($2.00) + 30,000 requests ($0.03) = $2.03
        cost = _kms_cost(keys_count=2, api_requests=30000,
                         catalog=catalog, region="us-east-1")
        assert cost == pytest.approx(2.03, rel=0.01)

    def test_pricing_zero_usage(self):
        catalog = PricingCatalog(seed=True)
        cost = _kms_cost(keys_count=0, api_requests=0,
                         catalog=catalog, region="us-east-1")
        assert cost == 0.0


class TestKMSRegistry:
    def test_registry_from_address(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_kms_key.main") == KMSKey

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_kms_key.main",
            "values": {"description": "primary key", "region": "us-east-1"},
        }
        result = ResourceRegistry.extract(
            "aws_kms_key.main", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AWSKMS"
        assert result["nodeType"] == "storage"
