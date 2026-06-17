"""Tests for DynamoDB resource model."""

import pytest
from infra_cost_model.resources.dynamodb import (
    DynamoDBTable, dynamodb_cost, _on_demand_cost, provisioned_cost
)


def test_dynamodb_from_address_terraform():
    """Test parsing Terraform DynamoDB address."""
    result = DynamoDBTable.from_address("aws_dynamodb_table.users")
    assert result is not None
    assert result.node_type == "storage"


def test_dynamodb_from_address_pulumi():
    """Test parsing Pulumi DynamoDB address."""
    result = DynamoDBTable.from_address("aws.dynamodb.Table:users")
    assert result is not None
    assert result.node_type == "storage"


def test_dynamodb_from_address_cdk():
    """Test parsing CDK DynamoDB address."""
    result = DynamoDBTable.from_address("MyStack/UsersTable/DynamoDB::Table")
    assert result is not None
    assert result.node_type == "storage"


def test_dynamodb_extract_tf():
    """Test Terraform extraction."""
    resource = {
        "address": "aws_dynamodb_table.users",
        "type": "aws_dynamodb_table",
        "values": {
            "billing_mode": "PAY_PER_REQUEST",
            "hash_key": "id",
            "range_key": "created_at",
            "region": "us-east-1"
        }
    }
    
    result = DynamoDBTable.extract_tf(resource)
    
    assert result.resource_address == "aws_dynamodb_table.users"
    assert result.node_type == "storage"
    assert result.provider == "aws"
    assert result.service == "AmazonDynamoDB"
    assert result.config["billingMode"] == "PAY_PER_REQUEST"
    assert result.config["hashKey"] == "id"


def test_dynamodb_extract_cdk():
    """Test CDK extraction."""
    resource = {
        "Type": "AWS::DynamoDB::Table",
        "LogicalId": "UsersTable",
        "Properties": {
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": [
                {"AttributeName": "id", "KeyType": "HASH"}
            ]
        }
    }
    
    result = DynamoDBTable.extract_cdk(resource)
    
    assert result.resource_address == "UsersTable"
    assert result.node_type == "storage"
    assert result.config["billingMode"] == "PAY_PER_REQUEST"
    assert result.config["hashKey"] == "id"


def test_dynamodb_on_demand_cost():
    """Test on-demand cost calculation."""
    cost = _on_demand_cost(1_000_000, 1_000_000, 10.0, None)
    
    # 1M reads = $1.25, 1M writes = $6.25, 10GB = $2.50
    expected = 1.25 + 6.25 + 2.50  # $10.00
    
    assert cost == pytest.approx(expected, rel=0.01)


def test_dynamodb_zero_cost():
    """Test zero cost for zero usage."""
    cost = dynamodb_cost(0, 0, 0)
    assert cost == 0


def test_dynamodb_storage_only():
    """Test storage-only cost."""
    cost = _on_demand_cost(0, 0, 100.0, None)
    
    # 100GB * $0.25 = $25
    assert cost == pytest.approx(25.0, rel=0.01)


def test_dynamodb_leaf_node_validation():
    """Test that DynamoDB is a leaf node (storage type)."""
    result = DynamoDBTable.from_address("aws_dynamodb_table.test")
    assert result is not None
    assert result.node_type == "storage"


def test_dynamodb_provisioned_cost():
    """Test provisioned cost from RCU/WCU hours."""
    cost = provisioned_cost(1000, 500, 10.0, None)
    
    # 1000 RCU-hours * $0.00013, 500 WCU-hours * $0.00065, 10GB * $0.25
    expected = 1000 * 0.00013 + 500 * 0.00065 + 10 * 0.25
    
    assert cost == pytest.approx(expected, rel=0.01)


def test_dynamodb_dynamodb_cost_provisioned():
    """Test dynamodb_cost with PROVISIONED billing mode."""
    cost = dynamodb_cost(1000, 500, 10.0, billing_mode="PROVISIONED", catalog=None)
    
    expected = 1000 * 0.00013 + 500 * 0.00065 + 10 * 0.25
    
    assert cost == pytest.approx(expected, rel=0.01)