"""AWS Lambda resource model implementation."""

from typing import Optional


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
