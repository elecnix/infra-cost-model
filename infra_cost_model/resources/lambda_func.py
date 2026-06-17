"""AWS Lambda resource model implementation."""

from typing import Optional
from dataclasses import dataclass

from .types import ComputeResource, ResourceExtract, UsageParams


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
                                 request_price_per_million: float = 0.20e-6) -> float:
    """Calculate fixed provisioned-concurrency cost plus request charges."""
    gb = memory_mb / 1024
    fixed_cost = provisioned_concurrency * gb * hours * 3600 * 0.000003606
    request_cost = invocations * request_price_per_million
    return fixed_cost + request_cost


def lambda_cost(invocations: float, memory_mb: float, avg_duration_ms: float,
                catalog=None) -> float:
    """Calculate Lambda cost with optional pricing catalog lookup.
    
    Args:
        invocations: Monthly invocations
        memory_mb: Allocated memory in MB
        avg_duration_ms: Average duration per invocation in ms
        catalog: Optional PricingCatalog for pricing lookup
        
    Returns:
        Total monthly cost in USD.
    """
    gb_seconds = calculate_gb_seconds(invocations, avg_duration_ms, memory_mb)
    billed_invocations, billed_gb_seconds = apply_free_tier(invocations, gb_seconds)
    
    if catalog:
        # Use catalog pricing
        request_price = catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-Request", invocations)
        duration_price = catalog.query("aws", "AWSLambda", "us-east-1", "Lambda-GB-Second", gb_seconds)
        
        cost = 0.0
        if request_price and hasattr(request_price, 'total_cost'):
            cost += request_price.total_cost
        if duration_price and hasattr(duration_price, 'total_cost'):
            cost += duration_price.total_cost
        return cost
    
    # Fallback prices
    request_cost = billed_invocations * 0.20e-6  # $0.20 per million
    duration_cost = billed_gb_seconds * 0.0000166667  # $0.00001667 per GB-s
    
    return request_cost + duration_cost