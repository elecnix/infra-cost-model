"""Bedrock/LLM Model resource model implementation."""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog

from .types import ComputeResource, ResourceExtract


class BedrockModel(ComputeResource):
    """Bedrock LLM model - compute node with token-based costing.

    LLM nodes are leaf nodes where input tokens flow in and output tokens flow out,
    both billable at different rates.
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["invocations", "inputTokens", "outputTokens"]

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


# The helpers query the same catalog rows as the engine (#320).
_METRICS = BedrockModel().catalog_metrics
_INPUT_TOKEN = _METRICS["inputTokens"]
_CACHED_INPUT_TOKEN = _METRICS["cachedReadTokens"]
_OUTPUT_TOKEN = _METRICS["outputTokens"]


def _bedrock_cost(input_tokens: float, output_tokens: float, model: str = "claude-3-5-sonnet", *,
                  catalog=None, provider: str = "aws", region: str) -> float:
    """Calculate Bedrock/LLM model cost using catalog prices.

    Args:
        input_tokens: Number of input tokens
        output_tokens: Number of output tokens
        model: Model identifier (affects pricing rates)
        catalog: Optional PricingCatalog (uses default if None, auto-loads seed)
        region: AWS region for pricing lookup

    Returns:
        Total cost in USD.
    """
    return _bedrock_token_cost(input_tokens, 0.0, output_tokens, model, catalog=catalog, provider=provider, region=region)


def _cached_prompt_bedrock_cost(input_tokens: float, cached_input_tokens: float,
                                output_tokens: float, *,
                                model: str = "claude-3-5-sonnet",
                                catalog=None, provider: str = "aws",
                                region: str) -> float:
    """Calculate Bedrock cost with cached prompt input discounted at 50%."""
    if catalog is None:
        catalog = PricingCatalog()

    cached_input_tokens = min(max(cached_input_tokens, 0), input_tokens)
    uncached_input_tokens = input_tokens - cached_input_tokens

    # Get cached input token price (50% discount)
    cached_cost = 0.0
    if cached_input_tokens > 0:
        cached_result = catalog.query(provider, "AmazonBedrock", region,
                                      _CACHED_INPUT_TOKEN, cached_input_tokens)
        if cached_result and hasattr(cached_result, 'total_cost'):
            cached_cost = cached_result.total_cost

    uncached_result = catalog.query(provider, "AmazonBedrock", region,
                                    _INPUT_TOKEN, uncached_input_tokens)
    output_result = catalog.query(provider, "AmazonBedrock", region,
                                  _OUTPUT_TOKEN, output_tokens)

    total = cached_cost
    for result in [uncached_result, output_result]:
        if result and hasattr(result, 'total_cost'):
            total += result.total_cost
    return total


def _streaming_bedrock_cost(input_tokens: float, output_tokens: float, *,
                            model: str = "claude-3-5-sonnet",
                            catalog=None, provider: str = "aws",
                            region: str) -> float:
    """Streaming delivery does not change total token cost."""
    return _bedrock_cost(input_tokens, output_tokens, model, catalog=catalog, provider=provider, region=region)


def _bedrock_token_cost(uncached_input_tokens: float, cached_input_tokens: float,
                        output_tokens: float, model: str, *, catalog, provider: str = "aws", region: str) -> float:
    """Internal: calculate Bedrock token cost using catalog prices."""
    if catalog is None:
        catalog = PricingCatalog()

    input_cost = 0.0
    cached_cost = 0.0
    output_cost = 0.0

    result = catalog.query(provider, "AmazonBedrock", region,
                          _INPUT_TOKEN, uncached_input_tokens)
    if result and hasattr(result, 'total_cost'):
        input_cost = result.total_cost

    result = catalog.query(provider, "AmazonBedrock", region,
                          _CACHED_INPUT_TOKEN, cached_input_tokens)
    if result and hasattr(result, 'total_cost'):
        cached_cost = result.total_cost

    result = catalog.query(provider, "AmazonBedrock", region,
                          _OUTPUT_TOKEN, output_tokens)
    if result and hasattr(result, 'total_cost'):
        output_cost = result.total_cost

    return input_cost + cached_cost + output_cost


def _model_cost_comparison(input_tokens: float, output_tokens: float, *, provider: str = "aws", region: str) -> dict:
    """Compare costs across LLM models.

    Note: Uses seed prices for comparison. The caller passes the region, as
    with every other cost helper (#164).
    """
    from infra_cost_model.pricing.catalog import PricingCatalog

    catalog = PricingCatalog()
    results = {}

    # Compare using catalog prices for different model pricing
    for model_name, metric_suffix in [
        ("claude-3-5-sonnet", ""),
        ("claude-3-5-haiku", ""),
        ("claude-3-opus", ""),
    ]:
        input_result = catalog.query(provider, "AmazonBedrock", region,
                                      _INPUT_TOKEN, input_tokens)
        output_result = catalog.query(provider, "AmazonBedrock", region,
                                      _OUTPUT_TOKEN, output_tokens)

        total = 0.0
        if input_result and hasattr(input_result, 'total_cost'):
            total += input_result.total_cost
        if output_result and hasattr(output_result, 'total_cost'):
            total += output_result.total_cost

        results[model_name] = total

    return results
