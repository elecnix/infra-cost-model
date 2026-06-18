"""Tests for API Gateway HTTP API v2 resource model."""

import pytest
from infra_cost_model.resources.apigw import (
    APIGatewayHTTP, _apigw_total_cost, _apigw_egress_cost, _request_cost, _egress_cost
)


def test_apigw_from_address_terraform():
    """Test parsing Terraform API Gateway v2 address."""
    result = APIGatewayHTTP.from_address("aws_apigatewayv2_api.my_api")
    assert result is not None
    assert result.node_type == "routing"


def test_apigw_from_address_pulumi():
    """Test parsing Pulumi API Gateway v2 address."""
    result = APIGatewayHTTP.from_address("aws.apigatewayv2.Api:my-api")
    assert result is not None
    assert result.node_type == "routing"


def test_apigw_from_address_cdk():
    """Test parsing CDK API Gateway v2 address."""
    result = APIGatewayHTTP.from_address("MyStack/HttpApi/APIGatewayV2::Api")
    assert result is not None
    assert result.node_type == "routing"


def test_apigw_extract_tf():
    """Test Terraform extraction with HTTP protocol type."""
    resource = {
        "address": "aws_apigatewayv2_api.my_api",
        "type": "aws_apigatewayv2_api",
        "values": {
            "protocol_type": "HTTP",
            "api_key_required": False,
            "endpoint_type": "REGIONAL",
            "region": "us-east-1"
        }
    }
    
    result = APIGatewayHTTP.extract_tf(resource)
    
    assert result.resource_address == "aws_apigatewayv2_api.my_api"
    assert result.node_type == "routing"
    assert result.provider == "aws"
    assert result.service == "AmazonAPIGatewayHTTP"
    assert result.config["protocolType"] == "HTTP"


def test_apigw_extract_cdk():
    """Test CDK extraction."""
    resource = {
        "Type": "AWS::ApiGatewayV2::Api",
        "LogicalId": "MyHttpApi",
        "Properties": {
            "ProtocolType": "HTTP"
        }
    }
    
    result = APIGatewayHTTP.extract_cdk(resource)
    
    assert result.resource_address == "MyHttpApi"
    assert result.node_type == "routing"
    assert result.config["protocolType"] == "HTTP"


def test_apigw_request_cost():
    """Test API Gateway HTTP API request cost calculation."""
    cost = _request_cost(1_000_000)  # 1M requests
    
    assert cost == pytest.approx(1.00, rel=0.01)


def test_apigw_egress_cost():
    """Test API Gateway egress cost calculation."""
    cost = _apigw_egress_cost(100)  # 100GB out (first tier)
    
    assert cost == pytest.approx(9.00, rel=0.01)


def test_apigw_egress_tiered_10tb():
    """Test egress cost at exactly 10TB boundary."""
    cost = _apigw_egress_cost(10_000)
    
    assert cost == pytest.approx(900.00, rel=0.01)


def test_apigw_egress_tiered_50tb():
    """Test egress cost in second tier (10-50TB)."""
    cost = _apigw_egress_cost(25_000)  # 25TB
    
    # 10TB at $0.09 + 15TB at $0.085
    expected = 10_000 * 0.09 + 15_000 * 0.085
    assert cost == pytest.approx(expected, rel=0.01)


def test_apigw_total_cost():
    """Test total cost includes both requests and egress."""
    cost = _apigw_total_cost(1_000_000, 100)
    
    expected = 1.00 + 9.00  # $1 requests + $9 egress
    assert cost == pytest.approx(expected, rel=0.01)


def test_apigw_routing_node():
    """Test that API Gateway is a routing node (can have outgoing edges)."""
    result = APIGatewayHTTP.from_address("aws_apigatewayv2_api.test")
    assert result is not None
    assert result.node_type == "routing"