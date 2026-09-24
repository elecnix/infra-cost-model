"""Amazon CloudFront Distribution resource model.

CloudFront is the CDN/routing node with tiered data transfer pricing.
Pricing: HTTP $0.0075/10K, HTTPS $0.0100/10K, Data out $0.085/GB.
The always-free tier covers the first 10 million HTTP or HTTPS requests and
the first 1 TB of data transfer out each month (#333).
CloudFront doesn't charge per origin fetch, and data transfer from an AWS
origin is free (#325).
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import RoutingResource, ResourceExtract


class CloudFrontDistribution(RoutingResource):
    """Amazon CloudFront Distribution - routing/CDN node with tiered pricing."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["requests", "dataOutGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudFrontDistribution"]:
        if (resource_address.startswith("aws_cloudfront_distribution.") or
                resource_address.startswith("aws.cloudfront.Distribution:") or
                resource_address.startswith("aws:cloudfront:Distribution:") or
                "CloudFront::Distribution:" in resource_address):
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


def _cloudfront_cost(requests=0, https_ratio=1.0, data_out_gb=0, *, catalog=None,
                     provider: str = "aws", region: str) -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    # The free 10 million requests cover HTTP and HTTPS together (#333), so
    # each protocol pays its share of the price of all the requests.
    for metric, share in (("CloudFront-HTTP-Request", 1.0 - https_ratio),
                          ("CloudFront-HTTPS-Request", https_ratio)):
        if requests > 0 and share > 0:
            r = catalog.query(provider, "AmazonCloudFront", region, metric, requests)
            if r and hasattr(r, "total_cost"): total += r.total_cost * share
    if data_out_gb > 0:
        r = catalog.query(provider, "AmazonCloudFront", region, "CloudFront-DataTransfer", data_out_gb)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    return total
