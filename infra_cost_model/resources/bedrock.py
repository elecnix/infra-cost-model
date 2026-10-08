"""Bedrock/LLM Model resource model implementation."""

from typing import Optional

from .types import ComputeResource, ResourceExtract


class BedrockModel(ComputeResource):
    """Bedrock LLM model - compute node with token-based costing.

    LLM nodes are leaf nodes where input tokens flow in and output tokens flow out,
    both billable at different rates.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "inputTokens", "outputTokens",
                "cachedReadTokens", "cacheWriteTokens"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        """Token names in the model map to the AmazonBedrock catalog rows (#312).

        ``cacheWriteTokens`` has no seed row, so it keeps its ``pricingRates``
        fallback.
        """
        return {
            "inputTokens": "Bedrock-Input-Token",
            "cachedReadTokens": "Bedrock-Cached-Input-Token",
            "outputTokens": "Bedrock-Output-Token",
        }

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["BedrockModel"]:
        """Parse resource address to determine if it's a Bedrock model."""
        if resource_address.startswith(("bedrock_model.", "aws_bedrock_model.")) or \
           resource_address.startswith("aws.bedrock.Model:") or \
           "Bedrock::Model:" in resource_address:
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """Extract from Terraform bedrock_model resource (logical - no direct resource).

        The node uses the vendor and service of the AmazonBedrock seed rows, so
        it finds its token prices (#320). The model id stays in the config.
        """
        values = resource.get("values", {})

        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="compute",
            provider="aws",
            service="AmazonBedrock",
            region=values.get("region"),
            config={
                "modelId": values.get("model_id"),
            }
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        """Extract from Pulumi bedrock model resource (logical)."""
        inputs = resource.get("inputs", {})

        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="compute",
            provider="aws",
            service="AmazonBedrock",
            region=inputs.get("region"),
            config={
                "modelId": inputs.get("modelId"),
            }
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        """Extract from CDK Bedrock model (logical)."""
        properties = resource.get("Properties", {})

        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="compute",
            provider="aws",
            service="AmazonBedrock",
            region=None,
            config={
                "modelId": properties.get("ModelId"),
            }
        )



