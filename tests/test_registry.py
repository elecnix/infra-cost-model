"""Tests for ResourceType registry."""

import pytest
from infra_cost_model.resources.registry import (
    ResourceRegistry,
    extract_resources_from_tf,
    extract_resources_from_pulumi,
    known_node_types,
    is_leaf_node,
)


def test_registry_from_lambda_address():
    """Test finding handler for Lambda address."""
    handler = ResourceRegistry.from_address("aws_lambda_function.get_items")
    assert handler is not None
    # node_type is accessed via instance, create one
    instance = handler.from_address("aws_lambda_function.get_items")
    assert instance.node_type == "compute"


def test_registry_from_dynamodb_address():
    """Test finding handler for DynamoDB address."""
    handler = ResourceRegistry.from_address("aws_dynamodb_table.users")
    assert handler is not None
    instance = handler.from_address("aws_dynamodb_table.users")
    assert instance.node_type == "storage"


def test_registry_from_external_address():
    """Test finding handler for external address."""
    handler = ResourceRegistry.from_address("external.stripe")
    assert handler is not None
    instance = handler.from_address("external.stripe")
    assert instance.node_type == "external"


def test_registry_unknown_address():
    """Test unknown address returns None."""
    result = ResourceRegistry.from_address("unknown.resource_type")
    assert result is None


def test_registry_extract_terraform():
    """Test extraction from Terraform resource."""
    resource = {
        "address": "aws_lambda_function.get_items",
        "type": "aws_lambda_function",
        "values": {
            "memory_size": 256,
            "timeout": 30,
            "region": "us-east-1",
        }
    }
    
    result = ResourceRegistry.extract(
        "aws_lambda_function.get_items",
        resource,
        "terraform"
    )
    
    assert result is not None
    assert result["nodeType"] == "compute"
    assert result["resourceAddress"] == "aws_lambda_function.get_items"


def test_registry_extract_external():
    """Test extraction of external node (should return None)."""
    resource = {
        "address": "stripe",
        "type": "external",
    }
    
    result = ResourceRegistry.extract("external.stripe", resource, "terraform")
    # External nodes can't be extracted - they have no IaC resource
    # But we can create them in the cost model directly
    assert result is None


def test_known_node_types():
    """Test known node types list."""
    types = known_node_types()
    assert "compute" in types
    assert "storage" in types
    assert "routing" in types
    assert "external" in types


def test_is_leaf_node():
    """Test leaf node detection."""
    assert is_leaf_node("storage") is True
    assert is_leaf_node("external") is True
    assert is_leaf_node("compute") is False
    assert is_leaf_node("routing") is False


def test_extract_resources_from_tf():
    """Test extracting multiple resources from Terraform JSON."""
    tf_json = {
        "resource": [
            {
                "address": "aws_lambda_function.get_items",
                "type": "aws_lambda_function",
                "values": {"memory_size": 256}
            },
            {
                "address": "aws_dynamodb_table.items",
                "type": "aws_dynamodb_table",
                "values": {"billing_mode": "PAY_PER_REQUEST"}
            }
        ]
    }
    
    results = extract_resources_from_tf(tf_json)
    
    assert "aws_lambda_function.get_items" in results
    assert "aws_dynamodb_table.items" in results


def test_extract_resources_from_tf_unsupported_warns():
    """Test that unsupported resources emit a warning during TF extraction."""
    import warnings
    
    tf_json = {
        "resource": [
            {
                "address": "aws_lambda_function.get_items",
                "type": "aws_lambda_function",
                "values": {"memory_size": 256}
            },
            {
                "address": "aws_eks_cluster.main",
                "type": "aws_eks_cluster",
                "values": {"name": "my-cluster"}
            },
            {
                "address": "aws_rds_cluster.main",
                "type": "aws_rds_cluster",
                "values": {"engine": "aurora"}
            }
        ]
    }
    
    with pytest.warns(UserWarning, match="could not be extracted"):
        results = extract_resources_from_tf(tf_json)
    
    # Lambda should be extracted, EKS and RDS are unsupported
    assert "aws_lambda_function.get_items" in results
    assert "aws_eks_cluster.main" not in results
    assert "aws_rds_cluster.main" not in results


def test_extract_resources_from_tf_all_supported_no_warning():
    """Test that no warning is emitted when all resources are supported."""
    import warnings
    
    tf_json = {
        "resource": [
            {
                "address": "aws_lambda_function.handler",
                "type": "aws_lambda_function",
                "values": {"memory_size": 256}
            },
            {
                "address": "aws_dynamodb_table.users",
                "type": "aws_dynamodb_table",
                "values": {"billing_mode": "PAY_PER_REQUEST"}
            }
        ]
    }
    
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        results = extract_resources_from_tf(tf_json)
    
    unsupported_warnings = [w for w in record if "could not be extracted" in str(w.message)]
    assert len(unsupported_warnings) == 0
    assert len(results) == 2


def test_extract_resources_from_tf_unsupported_lists_addresses():
    """Test that the warning message lists unsupported resource addresses."""
    tf_json = {
        "resource": [
            {
                "address": "aws_eks_cluster.main",
                "type": "aws_eks_cluster",
                "values": {}
            },
            {
                "address": "aws_rds_cluster.main",
                "type": "aws_rds_cluster",
                "values": {}
            }
        ]
    }
    
    with pytest.warns(UserWarning) as w:
        extract_resources_from_tf(tf_json)
    
    warning_msg = str(w[0].message)
    assert "aws_eks_cluster.main" in warning_msg
    assert "aws_rds_cluster.main" in warning_msg


def test_extract_resources_from_pulumi_unsupported_warns():
    """Test that unsupported Pulumi resources emit a warning."""
    pulumi_json = {
        "deployment": {
            "resources": [
                {
                    "id": "aws:lambda:Function:get-items",
                    "type": "aws:lambda:Function",
                    "inputs": {"memorySize": 256}
                },
                {
                    "id": "aws:eks:Cluster:main",
                    "type": "aws:eks:Cluster",
                    "inputs": {"name": "my-cluster"}
                }
            ]
        }
    }
    
    with pytest.warns(UserWarning, match="could not be extracted"):
        results = extract_resources_from_pulumi(pulumi_json)
    
    # Lambda should be extracted, EKS is unsupported
    assert "aws:lambda:Function:get-items" in results
    assert "aws:eks:Cluster:main" not in results


def test_known_prefixes():
    """Test that known_prefixes returns handler names."""
    prefixes = ResourceRegistry.known_prefixes()
    assert len(prefixes) >= 5  # We have at least 5 registered handlers
    assert "LambdaFunction" in prefixes
    assert "DynamoDBTable" in prefixes


def test_extract_resources_from_pulumi():
    """Test extracting multiple resources from Pulumi JSON."""
    pulumi_json = {
        "deployment": {
            "resources": [
                {
                    "id": "aws:lambda:Function:get-items",
                    "type": "aws:lambda:Function",
                    "inputs": {"memorySize": 256}
                },
                {
                    "id": "aws:dynamodb:Table:items",
                    "type": "aws:dynamodb:Table",
                    "inputs": {"billingMode": "PAY_PER_REQUEST"}
                }
            ]
        }
    }
    
    results = extract_resources_from_pulumi(pulumi_json)
    
    assert len(results) >= 1

class TestCdkExtraction:
    """Tests for CDK resource extraction."""

    def test_extract_cdk_lambda(self):
        """Extract Lambda from CDK CloudFormation template."""
        from infra_cost_model.resources.registry import extract_resources_from_cdk

        cdk_json = {
            "Resources": {
                "MyFunction": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {
                        "Handler": "index.handler",
                        "Runtime": "python3.9",
                        "MemorySize": 256,
                        "Timeout": 30,
                    },
                }
            }
        }

        nodes = extract_resources_from_cdk(cdk_json)
        assert "AWS::Lambda::Function:MyFunction" in nodes
        assert nodes["AWS::Lambda::Function:MyFunction"]["nodeType"] == "compute"

    def test_extract_cdk_dynamodb(self):
        """Extract DynamoDB from CDK CloudFormation template."""
        from infra_cost_model.resources.registry import extract_resources_from_cdk

        cdk_json = {
            "Resources": {
                "MyTable": {
                    "Type": "AWS::DynamoDB::Table",
                    "Properties": {
                        "TableName": "my-table",
                        "BillingMode": "PAY_PER_REQUEST",
                    },
                }
            }
        }

        nodes = extract_resources_from_cdk(cdk_json)
        assert "AWS::DynamoDB::Table:MyTable" in nodes
        assert nodes["AWS::DynamoDB::Table:MyTable"]["nodeType"] == "storage"

    def test_extract_cdk_apigw(self):
        """Extract API Gateway from CDK CloudFormation template."""
        from infra_cost_model.resources.registry import extract_resources_from_cdk

        cdk_json = {
            "Resources": {
                "MyApi": {
                    "Type": "AWS::ApiGatewayV2::Api",
                    "Properties": {
                        "Name": "my-api",
                        "ProtocolType": "HTTP",
                    },
                }
            }
        }

        nodes = extract_resources_from_cdk(cdk_json)
        assert "AWS::ApiGatewayV2::Api:MyApi" in nodes
        assert nodes["AWS::ApiGatewayV2::Api:MyApi"]["nodeType"] == "routing"

    def test_extract_cdk_unsupported_warns(self):
        """Unsupported CDK resource types produce warning."""
        from infra_cost_model.resources.registry import extract_resources_from_cdk

        cdk_json = {
            "Resources": {
                "MyFn": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {"MemorySize": 128},
                },
                "MyBucket": {
                    "Type": "AWS::RDS::DBCluster",
                    "Properties": {"Engine": "aurora"},
                },
            }
        }

        with pytest.warns(UserWarning, match="1 resource"):
            nodes = extract_resources_from_cdk(cdk_json)

        assert "AWS::Lambda::Function:MyFn" in nodes
        # RDS DBCluster is not a registered resource type
        assert "AWS::RDS::DBCluster:MyBucket" not in nodes

    def test_extract_cdk_empty_resources(self):
        """Empty CDK template returns empty dict."""
        from infra_cost_model.resources.registry import extract_resources_from_cdk

        cdk_json = {"Resources": {}}
        nodes = extract_resources_from_cdk(cdk_json)
        assert nodes == {}
