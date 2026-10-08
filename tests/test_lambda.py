"""Tests for AWS Lambda resource model."""

import pytest
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.resources.lambda_func import (
    LambdaFunction, calculate_gb_seconds,
)
from live_pricing import derived_resource_cost


def _lambda_cost(invocations=0, memory_mb=0, avg_duration_ms=0, *,
                 catalog=None, region="us-east-1"):
    """Price a Lambda invocation through the quantities the handler derives.

    Lambda bills requests and GB-seconds, and duration and memory only feed the
    GB-seconds formula, so the handler derives both catalog rows from the
    usage a model states.
    """
    return derived_resource_cost(
        "aws_lambda_function.fn", "AWSLambda", region,
        {"invocations": invocations, "avgDurationMs": avg_duration_ms,
         "memoryMb": memory_mb},
        catalog=catalog or PricingCatalog(seed=True),
    )


def _provisioned_concurrency_cost(provisioned_concurrency=0, hours=0,
                                  memory_mb=0, invocations=0, *,
                                  catalog=None, region="us-east-1"):
    """Price provisioned concurrency plus the requests it still bills for.

    Provisioned concurrency has its own catalog row, which the handler declares
    no logical metric for yet, so the row is named directly here.
    """
    catalog = catalog or PricingCatalog(seed=True)
    rate = catalog.query(
        "aws", "AWSLambda", region,
        "Lambda-ProvisionedConcurrency-GB-Second").price_usd
    gb = memory_mb / 1024
    fixed = provisioned_concurrency * gb * hours * 3600 * rate
    requests = catalog.query(
        "aws", "AWSLambda", region, "Lambda-Request", invocations).total_cost
    return fixed + requests


def test_lambda_from_address_terraform():
    """Test parsing Terraform Lambda address."""
    result = LambdaFunction.from_address("aws_lambda_function.get_items")
    assert result is not None
    assert result.node_type == "compute"


def test_lambda_from_address_pulumi():
    """Test parsing Pulumi Lambda address."""
    result = LambdaFunction.from_address("aws:lambda:Function:get-items")
    assert result is not None
    assert result.node_type == "compute"


def test_lambda_from_address_cdk():
    """Test parsing CDK Lambda address."""
    result = LambdaFunction.from_address("AWS::Lambda::Function:GetItems")
    assert result is not None
    assert result.node_type == "compute"


def test_lambda_extract_tf():
    """Test Terraform extraction."""
    resource = {
        "address": "aws_lambda_function.get_items",
        "type": "aws_lambda_function",
        "values": {
            "memory_size": 256,
            "timeout": 30,
            "runtime": "python3.12",
            "region": "us-east-1"
        },
        "name": "get_items"
    }

    result = LambdaFunction.extract_tf(resource)

    assert result.resource_address == "aws_lambda_function.get_items"
    assert result.node_type == "compute"
    assert result.provider == "aws"
    assert result.service == "AWSLambda"
    assert result.config["memoryMb"] == 256
    assert result.config["timeout"] == 30


def test_lambda_extract_pulumi():
    """Test Pulumi extraction."""
    resource = {
        "id": "aws:lambda:Function:get-items",
        "type": "aws:lambda:Function",
        "inputs": {
            "memorySize": 512,
            "timeout": 60,
            "runtime": "nodejs20.x",
            "region": "us-west-2"
        }
    }

    result = LambdaFunction.extract_pulumi(resource)

    assert result.resource_address == "aws:lambda:Function:get-items"
    assert result.node_type == "compute"
    assert result.config["memoryMb"] == 512


def test_lambda_extract_cdk():
    """Test CDK extraction."""
    resource = {
        "Type": "AWS::Lambda::Function",
        "LogicalId": "GetItemsFunction",
        "Properties": {
            "MemorySize": 128,
            "Timeout": 10,
            "Runtime": "python3.12"
        }
    }

    result = LambdaFunction.extract_cdk(resource)

    assert result.resource_address == "GetItemsFunction"
    assert result.node_type == "compute"
    assert result.config["memoryMb"] == 128


def test_gb_seconds_calculation():
    """Test GB-seconds derived metric calculation."""
    # 1M invocations * 200ms * 256MB
    gb_s = calculate_gb_seconds(1_000_000, 200, 256)

    # Expected: (256/1024) * (200/1000) * 1M = 0.25 * 0.2 * 1M = 50,000 GB-s
    assert gb_s == 50_000


def test_gb_seconds_zero_invocations():
    """Test GB-seconds with zero invocations."""
    gb_s = calculate_gb_seconds(0, 200, 256)
    assert gb_s == 0




def test_lambda_cost_calculation(seed_catalog):
    """Test Lambda cost calculation with catalog (free tier from tiered pricing).

    The seed data models the free tier as a $0 first tier for both
    Lambda-Request and Lambda-GB-Second (DP#4: limits are data, not code).
    The helper passes full quantities; the catalog applies the free tier
    automatically via tiered pricing.
    """
    cost = _lambda_cost(10_000_000, 256, 200, catalog=seed_catalog, region="us-east-1")

    # Full quantities: 10M invocations, 500K GB-s (from 256MB, 200ms, 10M calls)
    # Catalog tiered pricing:
    #   Lambda-Request:  Tier 0 (0-1M at $0) + Tier 1 (1M+ at $0.20/M)
    #   Lambda-GB-Second: Tier 0 (0-400K at $0) + Tier 1 (400K+ at $0.0166667/GB-s)
    # Result: 9M requests = $1.80, 100K GB-s ≈ $1.67
    expected_invocations_cost = 9_000_000 * 0.20e-6  # $1.80
    expected_duration_cost = 100_000 * 0.0000166667  # ~$1.67

    expected = expected_invocations_cost + expected_duration_cost
    assert cost == pytest.approx(expected, rel=0.01)




def test_provisioned_concurrency_cost(seed_catalog):
    """Test fixed provisioned concurrency cost plus request charges."""
    cost = _provisioned_concurrency_cost(
        provisioned_concurrency=10,
        hours=24,
        memory_mb=256,
        invocations=5_000,
        catalog=seed_catalog,
        region="us-east-1",
    )

    rate_result = seed_catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-ProvisionedConcurrency-GB-Second")
    rate = rate_result.price_usd if rate_result else 0.0000041667
    fixed = 10 * (256 / 1024) * 24 * 3600 * rate
    requests = 5_000 * 0.20e-6

    assert cost == pytest.approx(fixed + requests, rel=0.01)
