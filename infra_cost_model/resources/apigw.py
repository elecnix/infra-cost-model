"""API Gateway HTTP API v2 resource model implementation."""

from typing import Optional
from .types import RoutingResource, ResourceExtract


class APIGatewayHTTP(RoutingResource):
    """API Gateway HTTP API v2 - routing node (can have outgoing edges).
    
    HTTP API v2 is ~71% cheaper than REST API v1 at $3.50/1M requests.
    """
    
    @property
    def valid_metrics(self) -> list[str]:
        return ["requests", "dataOutGb"]
    
    @classmethod
    def from_address(cls, resource_address: str) -> Optional["APIGatewayHTTP"]:
        """Parse resource address to determine if it's HTTP API v2."""
        if resource_address.startswith("aws_apigatewayv2_api.") or \
           resource_address.startswith("aws.apigatewayv2.Api:") or \
           "APIGatewayV2::Api" in resource_address:
            return cls()
        return None
    
    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """Extract from Terraform aws_apigatewayv2_api resource."""
        values = resource.get("values", {})
        
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing",
            provider="aws",
            service="AmazonAPIGatewayHTTP",
            region=values.get("region"),
            config={
                "protocolType": values.get("protocol_type"),
                "apiKeyRequired": values.get("api_key_required"),
                "endpointType": values.get("endpoint_type"),
            }
        )
    
    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        """Extract from Pulumi aws.apigatewayv2.Api resource."""
        inputs = resource.get("inputs", {})
        
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing",
            provider="aws",
            service="AmazonAPIGatewayHTTP",
            region=inputs.get("region"),
            config={
                "protocolType": inputs.get("protocolType"),
                "apiKeyRequired": inputs.get("apiKeyRequired"),
                "endpointType": inputs.get("endpointType"),
            }
        )
    
    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        """Extract from CDK CloudFormation APIGatewayV2::Api."""
        properties = resource.get("Properties", {})
        
        # HTTP vs REST API type
        protocol_type = properties.get("ProtocolType", "HTTP")
        
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing",
            provider="aws",
            service="AmazonAPIGatewayHTTP",
            region=None,
            config={
                "protocolType": protocol_type,
            }
        )


def apigw_cost(requests: float, data_out_gb: float = 0.0,
               catalog=None) -> float:
    """Calculate API Gateway HTTP API cost.
    
    Args:
        requests: Monthly API requests
        data_out_gb: Monthly data transfer out in GB
        catalog: Optional PricingCatalog for pricing lookup
        
    Returns:
        Total monthly cost in USD.
    """
    if catalog:
        result = catalog.query("aws", "AmazonAPIGatewayHTTP", "us-east-1", 
                              "APIGateway-HTTP-Request", requests)
        cost = result.total_cost if result and hasattr(result, 'total_cost') else 0.0
    else:
        # HTTP API v2: $1.00 per million requests
        cost = requests * 1.00e-6
    
    return cost


def apigw_egress_cost(data_out_gb: float, catalog=None) -> float:
    """Calculate API Gateway data transfer egress cost.
    
    Args:
        data_out_gb: Data transfer in GB (first 10TB at $0.09/GB)
        
    Returns:
        Monthly egress cost in USD.
    """
    if data_out_gb <= 0:
        return 0.0
    
    if catalog:
        # Would query egress pricing
        pass
    
    # HTTP API egress: $0.09/GB (first 10TB)
    # Tiered pricing for egress
    if data_out_gb <= 10_000:
        rate = 0.09
    else:
        # Subsequent tiers would be higher
        rate = 0.085  # Simplified
    
    return data_out_gb * rate