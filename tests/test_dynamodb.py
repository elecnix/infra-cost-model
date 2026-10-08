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


def _provisioned_cost(rcu_hours=0, wcu_hours=0, storage_gb=0, *,
                      catalog=None, region="us-east-1"):
    """Price a provisioned table from its capacity-unit hours (#441).

    A provisioned table bills each hour of each read and write capacity unit
    it provisions, its global secondary indexes' units included.
    """
    return resource_cost(
        "aws_dynamodb_table.t", "AmazonDynamoDB", region,
        catalog=catalog or PricingCatalog(seed=True),
        readCapacityUnitHours=rcu_hours,
        writeCapacityUnitHours=wcu_hours,
        storageGb=storage_gb,
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
    cost = _on_demand_cost(1_000_000, 1_000_000, 10.0, catalog=seed_catalog)

    # 1M reads = $0.125, 1M writes = $0.625, 10GB = $2.50
    expected = 0.125 + 0.625  # $0.75; the 10 GB are inside the free 25 GB

    assert cost == pytest.approx(expected, rel=0.01)


def test_dynamodb_zero_cost():
    """Test zero cost for zero usage."""
    cost = _on_demand_cost(0, 0, 0)
    assert cost == 0


def test_dynamodb_storage_only(seed_catalog):
    """Test storage-only cost."""
    cost = _on_demand_cost(0, 0, 100.0, catalog=seed_catalog)

    # 75 GB above the free 25 GB x $0.25 = $18.75
    assert cost == pytest.approx(18.75, rel=0.01)


def test_dynamodb_leaf_node_validation():
    """Test that DynamoDB is a leaf node (storage type)."""
    result = DynamoDBTable.from_address("aws_dynamodb_table.test")
    assert result is not None
    assert result.node_type == "storage"


def test_dynamodb_provisioned_cost(seed_catalog):
    """Test provisioned cost from RCU/WCU hours."""
    cost = _provisioned_cost(20_000, 19_100, 30.0, catalog=seed_catalog)

    # Above the free 18,600 unit-hours and 25 GB: 1,400 RCU-hours x $0.00013,
    # 500 WCU-hours x $0.00065 and 5 GB x $0.25.
    expected = 1400 * 0.00013 + 500 * 0.00065 + 5 * 0.25

    assert cost == pytest.approx(expected, rel=0.01)


def test_dynamodb_provisioned_metrics_are_declared():
    """A model can state provisioned capacity in the handler's own vocabulary."""
    handler = DynamoDBTable()
    assert {"readCapacityUnitHours", "writeCapacityUnitHours"} <= set(handler.valid_metrics)
    assert handler.catalog_metrics["readCapacityUnitHours"] == "Dynamo-RCU-Hour"
    assert handler.catalog_metrics["writeCapacityUnitHours"] == "Dynamo-WCU-Hour"


def test_dynamodb_gsi_on_demand_cost(seed_catalog):
    """Global secondary indexes add read and write request charges."""
    cost = _on_demand_cost(
        1_000_000,
        1_000_000,
        10.0,
        gsi_read_requests=500_000,
        gsi_write_requests=250_000, catalog=seed_catalog)

    # The 10 GB are inside the free 25 GB.
    expected = 1_500_000 * 0.125e-6 + 1_250_000 * 0.625e-6

    assert cost == pytest.approx(expected, rel=0.01)


def test_dynamodb_gsi_provisioned_cost(seed_catalog):
    """Global secondary indexes add their own provisioned capacity-unit hours."""
    cost = _provisioned_cost(20_000 + 100, 19_100 + 50, 30.0, catalog=seed_catalog)

    expected = 1500 * 0.00013 + 550 * 0.00065 + 5 * 0.25

    assert cost == pytest.approx(expected, rel=0.01)


@pytest.mark.parametrize("metric,free,rate,used", [
    # https://aws.amazon.com/dynamodb/pricing/provisioned/ (checked 2026-10-08):
    # 25 GB of storage and 25 RCUs and 25 WCUs, per Region, per payer account.
    # The price list states the capacity allowance as 18,600 unit-hours, 25
    # units for a 744-hour month.
    ("Dynamo-Storage", 25, 0.25, 125),
    ("Dynamo-RCU-Hour", 18_600, 0.00013, 20_000),
    ("Dynamo-WCU-Hour", 18_600, 0.00065, 20_000),
])
def test_the_seed_states_the_dynamodb_free_tier(seed_catalog, metric, free, rate, used):
    assert seed_catalog.query("aws", "AmazonDynamoDB", "us-east-1", metric,
                              free).total_cost == pytest.approx(0.0)
    assert seed_catalog.query("aws", "AmazonDynamoDB", "us-east-1", metric,
                              used).total_cost == pytest.approx((used - free) * rate)
