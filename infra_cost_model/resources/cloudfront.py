"""Amazon CloudFront Distribution resource model.

CloudFront is the CDN/routing node with tiered data transfer pricing.
Pricing: HTTP $0.0075/10K, HTTPS $0.0100/10K, Data out $0.085/GB,
Origin requests S3 $0.0075/10K, Custom $0.0120/10K.
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import RoutingResource, ResourceExtract


class CloudFrontDistribution(RoutingResource):
    """Amazon CloudFront Distribution - routing/CDN node with tiered pricing."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["requests", "dataOutGb", "originRequests"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudFrontDistribution"]:
        if (resource_address.startswith("aws_cloudfront_distribution.") or
                resource_address.startswith("aws.cloudfront.Distribution:") or
                resource_address.startswith("aws:cloudfront:Distribution:") or
                "CloudFront::Distribution" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="routing", provider="aws", service="AmazonCloudFront",
            region="global",
            config={
                "aliases": values.get("aliases", []),
                "priceClass": values.get("price_class", "PriceClass_All"),
                "enabled": values.get("enabled", True),
                "origins": cls._parse_tf_origins(values.get("origin", [])),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="routing", provider="aws", service="AmazonCloudFront",
            region="global",
            config={
                "aliases": inputs.get("aliases", []),
                "priceClass": inputs.get("priceClass", "PriceClass_All"),
                "enabled": inputs.get("enabled", True),
                "origins": cls._parse_pulumi_origins(inputs.get("origins", [])),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        config = properties.get("DistributionConfig", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="routing", provider="aws", service="AmazonCloudFront",
            region="global",
            config={
                "aliases": config.get("Aliases", []),
                "priceClass": config.get("PriceClass", "PriceClass_All"),
                "enabled": config.get("Enabled", True),
                "origins": cls._parse_cdk_origins(config.get("Origins", [])),
            },
        )

    @staticmethod
    def _parse_tf_origins(origins):
        return [{"id": o.get("origin_id", ""), "domain": o.get("domain_name", ""),
                 "protocol": o.get("origin_protocol_policy", "")}
                for o in origins if isinstance(o, dict)]

    @staticmethod
    def _parse_pulumi_origins(origins):
        return [{"id": o.get("originId", ""), "domain": o.get("domainName", ""),
                 "protocol": o.get("originProtocolPolicy", "")}
                for o in origins if isinstance(o, dict)]

    @staticmethod
    def _parse_cdk_origins(origins):
        return [{"id": o.get("Id", ""), "domain": o.get("DomainName", ""),
                 "protocol": o.get("OriginProtocolPolicy", "")}
                for o in origins if isinstance(o, dict)]


def _cloudfront_cost(requests=0, https_ratio=1.0, data_out_gb=0, origin_requests=0,
                     origin_is_s3=True, catalog=None) -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    region = "us-east-1"
    http_req = requests * (1.0 - https_ratio)
    https_req = requests * https_ratio
    if http_req > 0:
        r = catalog.query("aws", "AmazonCloudFront", region, "CloudFront-HTTP-Request", http_req)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    if https_req > 0:
        r = catalog.query("aws", "AmazonCloudFront", region, "CloudFront-HTTPS-Request", https_req)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    if data_out_gb > 0:
        r = catalog.query("aws", "AmazonCloudFront", region, "CloudFront-DataTransfer", data_out_gb)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    if origin_requests > 0:
        metric = "CloudFront-OriginRequest-S3" if origin_is_s3 else "CloudFront-OriginRequest-Custom"
        r = catalog.query("aws", "AmazonCloudFront", region, metric, origin_requests)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    return total
