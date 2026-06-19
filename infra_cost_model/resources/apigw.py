"""API Gateway HTTP API v2 resource model implementation."""

from infra_cost_model.pricing.catalog import PricingCatalog

from .types import RoutingResource, ResourceExtract


class APIGatewayHTTP(RoutingResource):
    """API Gateway HTTP API v2 - routing node (can have outgoing edges).
    
    HTTP API v2 pricing: $1.00/1M requests.
    """
    
    @property
    def valid_metrics(self) -> list[str]:
        return ["requests", "dataOutGb"]
    
    @classmethod
    def from_address(cls, resource_address: str) -> ResourceExtract | None:
        """Parse resource address to determine if it's HTTP API v2."""
        if resource_address.startswith("aws_apigatewayv2_api.") or \
           resource_address.startswith("aws.apigatewayv2.Api:") or \
           resource_address.startswith("aws:apigatewayv2:Api:") or \
           "ApiGatewayV2::Api" in resource_address or \
           "apigatewayv2::api" in resource_address.lower():
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
        protocol_type = properties.get("ProtocolType", "HTTP")
        
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing",
            provider="aws",
            service="AmazonAPIGatewayHTTP",
            region=None,
            config={"protocolType": protocol_type}
        )


def _apigw_total_cost(requests: float, data_out_gb: float = 0.0,
                      *,
                      region: str,
                      catalog=None) -> float:
    """Calculate total API Gateway HTTP API cost including egress.
    
    Args:
        requests: Monthly API requests
        data_out_gb: Monthly data transfer out in GB
        catalog: Optional PricingCatalog (uses default if None, auto-loads seed)
        region: AWS region for pricing lookup
        
    Returns:
        Total monthly cost in USD (requests + egress).
    """
    if catalog is None:
        catalog = PricingCatalog()
    
    request_cost = _request_cost(requests, region, catalog)
    egress_cost = _egress_cost(data_out_gb, region, catalog)
    return request_cost + egress_cost


def _request_cost(requests: float, region: str, catalog=None) -> float:
    """Calculate API Gateway request cost using catalog."""
    if catalog is None:
        catalog = PricingCatalog()
    result = catalog.query("aws", "AmazonAPIGatewayHTTP", region,
                           "APIGateway-HTTP-Request", requests)
    return result.total_cost if result and hasattr(result, 'total_cost') else 0.0


def _egress_cost(data_out_gb: float, region: str, catalog=None) -> float:
    """Calculate API Gateway egress cost with tiered pricing.
    
    Tiered egress (first 10TB at $0.09/GB):
    - 1-10 TB: $0.09/GB
    - Next 40 TB: $0.085/GB
    - Next 100 TB: $0.07/GB
    - Next 350 TB: $0.05/GB
    """
    if data_out_gb <= 0:
        return 0.0
    
    if catalog is None:
        catalog = PricingCatalog()
    
    result = catalog.query("aws", "AmazonAPIGateway", region,
                           "APIGateway-Egress", data_out_gb)
    return result.total_cost if result and hasattr(result, 'total_cost') else 0.0


def _apigw_egress_cost(data_out_gb: float, region: str, catalog=None) -> float:
    """Alias for egress cost calculation."""
    if catalog is None:
        catalog = PricingCatalog()
    return _egress_cost(data_out_gb, region, catalog)
