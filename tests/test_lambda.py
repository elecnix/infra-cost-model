"""Tests for AWS Lambda resource model."""

import pytest
from infra_cost_model.resources.lambda_func import (
    LambdaFunction, calculate_gb_seconds, apply_free_tier, lambda_cost,
    provisioned_concurrency_cost
)
from infra_cost_model.resources.types import ResourceExtract


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
    result = LambdaFunction.from_address("MyApi/GetItems/AWS::Lambda::Function")
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


def test_free_tier_application():
    """Test free tier deduction."""
    # 2M invocations
    billed = apply_free_tier(2_000_000, 500_000)
    
    assert billed[0] == 1_000_000  # 2M - 1M free
    assert billed[1] == 100_000  # 500K - 400K free


def test_free_tier_below_threshold():
    """Test free tier when usage is below threshold."""
    billed = apply_free_tier(500_000, 100_000)
    
    assert billed[0] == 0
    assert billed[1] == 0


def test_lambda_cost_calculation():
    """Test Lambda cost calculation."""
    # 10M invocations, 256MB, 200ms
    gb_s = calculate_gb_seconds(10_000_000, 200, 256)  # 500K GB-s
    
    cost = lambda_cost(10_000_000, 256, 200)
    
    # After free tier: 9M invocations, 100K GB-s billed
    expected_invocations_cost = 9_000_000 * 0.20e-6  # $0.18
    expected_duration_cost = 100_000 * 0.0000166667  # ~$1.67
    
    assert cost > 0
    assert cost < 10  # Should be reasonable

def test_provisioned_concurrency_cost():
    """Test fixed provisioned concurrency cost plus request charges."""
    cost = provisioned_concurrency_cost(
        provisioned_concurrency=10,
        hours=24,
        memory_mb=256,
        invocations=5_000,
    )

    fixed = 10 * (256 / 1024) * 24 * 3600 * 0.000003606
    requests = 5_000 * 0.20e-6

    assert cost == pytest.approx(fixed + requests, rel=0.01)
