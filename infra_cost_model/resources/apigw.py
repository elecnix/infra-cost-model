"""API Gateway HTTP API v2 resource model implementation."""


from .types import RoutingResource, ResourceExtract


class APIGatewayHTTP(RoutingResource):
    """API Gateway HTTP API v2 - routing node (can have outgoing edges).

    HTTP API v2 pricing: $1.00/1M requests.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["requests", "dataOutGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # AWS bills the response body as data transfer out, like S3 egress (#332).
        return {"requests": "APIGateway-HTTP-Request",
                "dataOutGb": "DataTransfer-Internet-Out-GB"}

    @property
    def catalog_services(self) -> dict[str, str]:
        return {"DataTransfer-Internet-Out-GB": "AWSDataTransfer"}

    @classmethod
    def from_address(cls, resource_address: str) -> ResourceExtract | None:
        """Parse resource address to determine if it's HTTP API v2."""
        if resource_address.startswith("aws_apigatewayv2_api.") or \
           resource_address.startswith("aws.apigatewayv2.Api:") or \
           resource_address.startswith("aws:apigatewayv2:Api:") or \
           "ApiGatewayV2::Api:" in resource_address or \
           "apigatewayv2::api:" in resource_address.lower():
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
