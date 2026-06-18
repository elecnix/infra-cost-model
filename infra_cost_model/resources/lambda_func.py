"""AWS Lambda resource model implementation."""

from typing import Optional

from infra_cost_model.pricing.catalog import PricingCatalog

from .types import ComputeResource, ResourceExtract


class LambdaFunction(ComputeResource):
    """AWS Lambda function - compute node with derived GB-seconds metric."""
    
    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "avgDurationMs", "memoryMb"]
    
    @classmethod
    def from_address(cls, resource_address: str) -> Optional["LambdaFunction"]:
        """Parse resource address to determine if it's a Lambda function."""
        if resource_address.startswith("aws_lambda_function.") or \
           resource_address.startswith("aws:lambda:Function:") or \
           ":Lambda::Function" in resource_address:
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
                    free_gb_seconds: float = 400_000) -> tuple[float, float]:
    """Apply Lambda free tier deductions.
    
    Returns:
        Tuple of (billed_invocations, billed_gb_seconds).
    """
    billed_invocations = max(0, invocations - free_requests)
    billed_gb_seconds = max(0, gb_seconds - free_gb_seconds)
    
    return billed_invocations, billed_gb_seconds


def provisioned_concurrency_cost(provisioned_concurrency: float, hours: float,
                                 memory_mb: float = 128,
                                 invocations: float = 0,
                                 catalog=None) -> float:
    """Calculate fixed provisioned-concurrency cost plus request charges.
    
    Args:
        provisioned_concurrency: Number of provisioned concurrent executions
        hours: Hours of provisioned concurrency
        memory_mb: Memory in MB (affects GB calculation)
        invocations: Number of invocations for request pricing
        catalog: Optional PricingCatalog (uses default if None)
        
    Returns:
        Total hourly provisioned concurrency cost plus request charges.
    """
    gb = memory_mb / 1024
    fixed_cost = provisioned_concurrency * gb * hours * 3600 * 0.000003606
    
    if catalog is None:
        catalog = PricingCatalog()
    
    request_price = catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-Request", invocations)
    
    if not request_price:
        raise PricingUnavailableError(
            "Lambda request pricing unavailable. "
            "Run 'infra-cost-model seed-pricing' first."
        )
    
    return fixed_cost + request_price.total_cost


def lambda_cost(invocations: float, memory_mb: float, avg_duration_ms: float,
                catalog=None, region: str = "us-east-1") -> float:
    """Calculate Lambda cost with pricing catalog lookup.
    
    Args:
        invocations: Monthly invocations
        memory_mb: Allocated memory in MB
        avg_duration_ms: Average duration per invocation in ms
        catalog: Optional PricingCatalog (uses default if None, auto-loads seed)
        region: AWS region for pricing lookup
        
    Returns:
        Total monthly cost in USD.
        
    Raises:
        PricingUnavailableError: If pricing data unavailable even after seed load.
    """
    gb_seconds = calculate_gb_seconds(invocations, avg_duration_ms, memory_mb)
    billed_invocations, billed_gb_seconds = apply_free_tier(invocations, gb_seconds)
    
    # Use default catalog that auto-loads seed if needed
    if catalog is None:
        catalog = PricingCatalog()
    
    # Query catalog for pricing (seed auto-loaded on first query)
    request_price = catalog.query("aws", "AWSLambda", region, "Lambda-Request", billed_invocations)
    duration_price = catalog.query("aws", "AWSLambda", region, "Lambda-GB-Second", billed_gb_seconds)
    
    cost = 0.0
    if request_price and hasattr(request_price, 'total_cost'):
        cost += request_price.total_cost
    else:
        raise PricingUnavailableError(
            f"Lambda request pricing unavailable for {region}. "
            "Run 'infra-cost-model seed-pricing' to initialize pricing data."
        )
    
    if duration_price and hasattr(duration_price, 'total_cost'):
        cost += duration_price.total_cost
    
    return cost


class PricingUnavailableError(RuntimeError):
    """Raised when pricing data is unavailable for cost calculation."""
    pass
