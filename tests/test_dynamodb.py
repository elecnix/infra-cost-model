"""Tests for DynamoDB resource model."""

import pytest
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.resources.dynamodb import DynamoDBTable
from live_pricing import resource_cost


def _on_demand_cost(read_requests=0, write_requests=0, storage_gb=0,
                    gsi_read_requests=0, gsi_write_requests=0, *,
                    catalog=None, region="us-east-1"):
    """Price a table billed per request, through the handler's catalog metrics."""
    return resource_cost(
        "aws_dynamodb_table.t", "AmazonDynamoDB", region,
        catalog=catalog or PricingCatalog(seed=True),
        readRequests=read_requests + gsi_read_requests,
        writeRequests=write_requests + gsi_write_requests,
        storageGb=storage_gb,
    )


def _provisioned_dynamodb_cost(rcu_hours=0, wcu_hours=0, storage_gb=0,
                               gsi_rcu_hours=0, gsi_wcu_hours=0, *,
                               catalog=None, region="us-east-1"):
    """Price a provisioned table from its RCU- and WCU-hour rows.

    A provisioned table bills hours rather than requests. The handler declares
    no logical metric for those two rows yet, so they are named directly here.
    """
    catalog = catalog or PricingCatalog(seed=True)
    total = 0.0
    for metric, hours in (("Dynamo-RCU-Hour", rcu_hours + gsi_rcu_hours),
                          ("Dynamo-WCU-Hour", wcu_hours + gsi_wcu_hours),
                          ("Dynamo-Storage", storage_gb)):
        if hours:
            total += catalog.query(
                "aws", "AmazonDynamoDB", region, metric, hours).total_cost
    return total


def _dynamodb_cost(read_requests=0, write_requests=0, storage_gb=0,
                   billing_mode="PAY_PER_REQUEST", **usage):
    """Price a table in whichever billing mode it declares."""
    if billing_mode == "PROVISIONED":
        return _provisioned_dynamodb_cost(
            read_requests, write_requests, storage_gb, **usage)
    return _on_demand_cost(read_requests, write_requests, storage_gb, **usage)


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
    result = DynamoDBTable.from_address("AWS::DynamoDB::Table:UsersTable")
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


def test_dynamodb_on_demand_cost(seed_catalog):
    """Test on-demand cost calculation."""
    cost = _on_demand_cost(1_000_000, 1_000_000, 10.0)

    # 1M reads = $0.125, 1M writes = $0.625, 10GB = $2.50
    expected = 0.125 + 0.625 + 2.50  # $3.25

    assert cost == pytest.approx(expected, rel=0.01)


def test_dynamodb_zero_cost():
    """Test zero cost for zero usage."""
    cost = _dynamodb_cost(0, 0, 0)
    assert cost == 0


def test_dynamodb_storage_only(seed_catalog):
    """Test storage-only cost."""
    cost = _on_demand_cost(0, 0, 100.0)

    # 100GB * $0.25 = $25
    assert cost == pytest.approx(25.0, rel=0.01)


def test_dynamodb_leaf_node_validation():
    """Test that DynamoDB is a leaf node (storage type)."""
    result = DynamoDBTable.from_address("aws_dynamodb_table.test")
    assert result is not None
    assert result.node_type == "storage"


def test_dynamodb_provisioned_cost(seed_catalog):
    """Test provisioned cost from RCU/WCU hours."""
    cost = _provisioned_dynamodb_cost(1000, 500, 10.0)

    # 1000 RCU-hours * $0.00013, 500 WCU-hours * $0.00065, 10GB * $0.25
    expected = 1000 * 0.00013 + 500 * 0.00065 + 10 * 0.25

    assert cost == pytest.approx(expected, rel=0.01)


def test_dynamodb_dynamodb_cost_provisioned(seed_catalog):
    """Test _dynamodb_cost with PROVISIONED billing mode."""
    cost = _dynamodb_cost(1000, 500, 10.0, billing_mode="PROVISIONED")

    expected = 1000 * 0.00013 + 500 * 0.00065 + 10 * 0.25

    assert cost == pytest.approx(expected, rel=0.01)

def test_dynamodb_gsi_on_demand_cost(seed_catalog):
    """Global secondary indexes add read and write request charges."""
    cost = _dynamodb_cost(
        1_000_000,
        1_000_000,
        10.0,
        gsi_read_requests=500_000,
        gsi_write_requests=250_000
    )

    expected = (
        1_500_000 * 0.125e-6
        + 1_250_000 * 0.625e-6
        + 10 * 0.25
    )

    assert cost == pytest.approx(expected, rel=0.01)


def test_dynamodb_gsi_provisioned_cost(seed_catalog):
    """Global secondary indexes add provisioned RCU/WCU-hour charges."""
    cost = _provisioned_dynamodb_cost(
        1000,
        500,
        10.0,
        gsi_rcu_hours=100,
        gsi_wcu_hours=50
    )

    expected = 1100 * 0.00013 + 550 * 0.00065 + 10 * 0.25

    assert cost == pytest.approx(expected, rel=0.01)
