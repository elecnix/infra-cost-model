"""AWS Lambda resource model implementation."""

from typing import Dict, Optional


from .types import ComputeResource, DerivedCatalogUsage, ResourceExtract


class LambdaFunction(ComputeResource):
    """AWS Lambda function - compute node with derived GB-seconds metric."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "avgDurationMs", "memoryMb"]

    def derive_catalog_usage(self, usage: dict[str, float],
                             config: Optional[dict] = None) -> Optional[DerivedCatalogUsage]:
        """Derive requests and GB-seconds, the quantities Lambda bills.

        Duration and memory have no price of their own. They feed the
        GB-seconds formula in ``calculate_gb_seconds``.
        """
        inputs = ("invocations", "avgDurationMs", "memoryMb")
        if not all(name in usage for name in inputs):
            return None
        invocations = usage["invocations"]
        return DerivedCatalogUsage(
            consumed=frozenset(inputs),
            quantities={
                "Lambda-Request": invocations,
                "Lambda-GB-Second": calculate_gb_seconds(
                    invocations, usage["avgDurationMs"], usage["memoryMb"]),
            },
        )

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["LambdaFunction"]:
        """Parse resource address to determine if it's a Lambda function."""
        if resource_address.startswith("aws_lambda_function.") or \
           resource_address.startswith("aws:lambda:Function:") or \
           ":Lambda::Function:" in resource_address:
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """Extract from Terraform aws_lambda_function resource."""
        values = resource.get("values", {})

        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="compute",
            provider="aws",
            service="AWSLambda",
            region=values.get("region"),
            config={
                "memoryMb": values.get("memory_size"),
                "timeout": values.get("timeout"),
                "runtime": values.get("runtime"),
            }
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        """Extract from Pulumi aws.lambda.Function resource."""
        inputs = resource.get("inputs", {})

        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="compute",
            provider="aws",
            service="AWSLambda",
            region=inputs.get("region"),
            config={
                "memoryMb": inputs.get("memorySize"),
                "timeout": inputs.get("timeout"),
                "runtime": inputs.get("runtime"),
            }
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        """Extract from CDK CloudFormation AWS::Lambda::Function."""
        properties = resource.get("Properties", {})

        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="compute",
            provider="aws",
            service="AWSLambda",
            region=None,
            config={
                "memoryMb": properties.get("MemorySize"),
                "timeout": properties.get("Timeout"),
                "runtime": properties.get("Runtime"),
            }
        )


def calculate_gb_seconds(invocations: float, avg_duration_ms: float, memory_mb: float) -> float:
    """Calculate GB-seconds from invocations, duration, and memory.

    Formula: (memoryMb / 1024) * (avgDurationMs / 1000) * invocations
    """
    if invocations <= 0:
        return 0.0

    return (memory_mb / 1024) * (avg_duration_ms / 1000) * invocations


def apply_free_tier(invocations: float, gb_seconds: float,
                    free_requests: float = 1_000_000,
                    free_gb_seconds: float = 400_000,
                    catalog=None, *, provider: str = "aws",
                    region: str) -> tuple[float, float]:
    """Apply Lambda free tier deductions.

    If a PricingCatalog is provided, free tier limits are queried from the
    catalog (DP#4: parameters as data, not code). Falls back to the
    hardcoded defaults when the catalog is unavailable or doesn't have
    the free tier data.

    Args:
        invocations: Monthly request count.
        gb_seconds: Monthly GB-second consumption.
        free_requests: Override free request limit (default: 1_000_000).
        free_gb_seconds: Override free GB-second limit (default: 400_000).
        catalog: Optional PricingCatalog for data-driven limits.
        provider: Cloud provider for the catalog lookup.
        region: Region for the catalog lookup, from the node metadata.

    Returns:
        Tuple of (billed_invocations, billed_gb_seconds).
    """
    limits = get_lambda_free_tier_limits(catalog=catalog, provider=provider, region=region)
    if limits:
        free_requests = limits.get("requests", free_requests)
        free_gb_seconds = limits.get("gb_seconds", free_gb_seconds)

    billed_invocations = max(0, invocations - free_requests)
    billed_gb_seconds = max(0, gb_seconds - free_gb_seconds)

    return billed_invocations, billed_gb_seconds


def get_lambda_free_tier_limits(*, catalog=None, provider: str = "aws", region: str) -> Optional[Dict[str, float]]:
    """Retrieve Lambda free tier limits from the pricing catalog.

    Per DP#4, free tier limits are first-class data, not hardcoded constants.
    The catalog's tiered pricing for Lambda-Request and Lambda-GB-Second
    includes a $0 first tier whose end_usage_amount is the free tier limit.

    Args:
        catalog: Optional PricingCatalog. If None, returns None.
        provider: Cloud provider for the catalog lookup.
        region: Region for the catalog lookup, from the node metadata.

    Returns:
        Dict with 'requests' and 'gb_seconds' keys, or None if unavailable.
    """
    if catalog is None:
        return None

    limits = {}

    # Query the catalog for Lambda-Request pricing. If tiered, the first
    # tier with price $0 and a non-None end_usage_amount gives the request
    # free tier limit.
    try:
        request_result = catalog.query(provider, "AWSLambda", region, "Lambda-Request")
        if request_result is not None:
            tiers = request_result.tiers if hasattr(request_result, 'tiers') else [request_result]
            for tier in sorted(tiers, key=lambda t: t.start_usage_amount or 0):
                if tier.price_usd == 0 and tier.end_usage_amount is not None:
                    limits["requests"] = float(tier.end_usage_amount)
                    break
    except Exception:
        pass

    # Query for Lambda-GB-Second free tier limit
    try:
        gbs_result = catalog.query(provider, "AWSLambda", region, "Lambda-GB-Second")
        if gbs_result is not None:
            tiers = gbs_result.tiers if hasattr(gbs_result, 'tiers') else [gbs_result]
            for tier in sorted(tiers, key=lambda t: t.start_usage_amount or 0):
                if tier.price_usd == 0 and tier.end_usage_amount is not None:
                    limits["gb_seconds"] = float(tier.end_usage_amount)
                    break
    except Exception:
        pass

    return limits if len(limits) == 2 else None


class PricingUnavailableError(RuntimeError):
    """Raised when pricing data is unavailable for cost calculation."""
    pass
