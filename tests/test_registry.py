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