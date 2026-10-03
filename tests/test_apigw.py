"""Tests for API Gateway HTTP API v2 resource model."""

import pytest
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.resources.apigw import APIGatewayHTTP
from live_pricing import resource_cost


def _request_cost(requests=0, *, catalog=None, region="us-east-1"):
    """Price an HTTP API's requests through the handler's catalog metrics."""
    return resource_cost("aws_apigatewayv2_api.api", "AmazonAPIGatewayHTTP",
                         region, catalog=catalog or PricingCatalog(seed=True),
                         requests=requests)


def _egress_cost(data_out_gb=0, *, catalog=None, region="us-east-1"):
    """Price an HTTP API's response bytes.

    API Gateway has no egress price of its own. AWS bills the bytes as data
    transfer out to the internet, which the handler maps to the shared
    ``DataTransfer-Internet-Out-GB`` rows (#311).
    """
    return resource_cost("aws_apigatewayv2_api.api", "AmazonAPIGatewayHTTP",
                         region, catalog=catalog or PricingCatalog(seed=True),
                         dataOutGb=data_out_gb)


_apigw_egress_cost = _egress_cost


def _apigw_total_cost(requests=0, data_out_gb=0, *, catalog=None, region="us-east-1"):
    return (_request_cost(requests, catalog=catalog, region=region)
            + _egress_cost(data_out_gb, catalog=catalog, region=region))


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
    result = APIGatewayHTTP.from_address("AWS::ApiGatewayV2::Api:HttpApi")
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


def test_apigw_request_cost(seed_catalog):
    """Test API Gateway HTTP API request cost calculation."""
    cost = _request_cost(1_000_000)  # 1M requests

    assert cost == pytest.approx(1.00, rel=0.01)


def test_apigw_egress_cost(seed_catalog):
    """Test API Gateway egress cost calculation."""
    cost = _apigw_egress_cost(200)  # 200GB out

    # The first 100 GB a month are free (#327), then $0.09 per GB
    assert cost == pytest.approx(9.00, rel=0.01)


def test_apigw_egress_tiered_10tb(seed_catalog):
    """Test egress cost at exactly 10TB boundary."""
    cost = _apigw_egress_cost(10_000)

    assert cost == pytest.approx((10_000 - 100) * 0.09, rel=0.01)


def test_apigw_egress_tiered_50tb(seed_catalog):
    """Test egress cost in second tier (10-50TB)."""
    cost = _apigw_egress_cost(25_000)  # 25TB

    # 100 GB free, up to 10 TB (10,240 GB) at $0.09, the rest at $0.085
    expected = (10_240 - 100) * 0.09 + (25_000 - 10_240) * 0.085
    assert cost == pytest.approx(expected, rel=0.01)


def test_apigw_total_cost(seed_catalog):
    """Test total cost includes both requests and egress."""
    cost = _apigw_total_cost(1_000_000, 200)

    expected = 1.00 + 9.00  # $1 requests + $9 egress (100 GB free, 100 GB paid)
    assert cost == pytest.approx(expected, rel=0.01)


def test_apigw_routing_node():
    """Test that API Gateway is a routing node (can have outgoing edges)."""
    result = APIGatewayHTTP.from_address("aws_apigatewayv2_api.test")
    assert result is not None
    assert result.node_type == "routing"