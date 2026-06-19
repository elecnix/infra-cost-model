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
    
    @classmethod
    def from_address(cls, resource_address: str) -> Optional["BedrockModel"]:
        """Parse resource address to determine if it's a Bedrock model."""
        if resource_address.startswith("bedrock_model.") or \
           resource_address.startswith("aws.bedrock.Model:") or \
           "Bedrock::Model" in resource_address:
            return cls()
        return None
    
    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """Extract from Terraform bedrock_model resource (logical - no direct resource)."""
        values = resource.get("values", {})
        
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="compute",
            provider="bedrock",
            service="Claude",  # or other model
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
            provider="bedrock",
            service="Claude",
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
            provider="bedrock",
            service="Claude",
            region=None,
            config={
                "modelId": properties.get("ModelId"),
            }
        )


def _bedrock_cost(input_tokens: float, output_tokens: float, model: str = "claude-3-5-sonnet",
                  catalog=None, provider: str = "aws", region: str = "us-east-1") -> float:
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
    return _bedrock_token_cost(input_tokens, 0.0, output_tokens, model, catalog, provider, region)


def _cached_prompt_bedrock_cost(input_tokens: float, cached_input_tokens: float,
                                output_tokens: float,
                                model: str = "claude-3-5-sonnet",
                                catalog=None, provider: str = "aws",
                                region: str = "us-east-1") -> float:
    """Calculate Bedrock cost with cached prompt input discounted at 50%."""
    if catalog is None:
        catalog = PricingCatalog()
    
    cached_input_tokens = min(max(cached_input_tokens, 0), input_tokens)
    uncached_input_tokens = input_tokens - cached_input_tokens
    
    # Get cached input token price (50% discount)
    cached_cost = 0.0
    if cached_input_tokens > 0:
        cached_result = catalog.query(provider, "AmazonBedrock", region,
                                      "Bedrock-Cached-Input-Token", cached_input_tokens)
        if cached_result and hasattr(cached_result, 'total_cost'):
            cached_cost = cached_result.total_cost
    
    uncached_result = catalog.query(provider, "AmazonBedrock", region,
                                    "Bedrock-Input-Token", uncached_input_tokens)
    output_result = catalog.query(provider, "AmazonBedrock", region,
                                  "Bedrock-Output-Token", output_tokens)
    
    total = cached_cost
    for result in [uncached_result, output_result]:
        if result and hasattr(result, 'total_cost'):
            total += result.total_cost
    return total


def _streaming_bedrock_cost(input_tokens: float, output_tokens: float,
                            model: str = "claude-3-5-sonnet",
                            catalog=None, provider: str = "aws",
                            region: str = "us-east-1") -> float:
    """Streaming delivery does not change total token cost."""
    return _bedrock_cost(input_tokens, output_tokens, model, catalog, provider, region)


def _bedrock_token_cost(uncached_input_tokens: float, cached_input_tokens: float,
                        output_tokens: float, model: str, catalog, provider: str = "aws", region: str = "us-east-1") -> float:
    """Internal: calculate Bedrock token cost using catalog prices."""
    if catalog is None:
        catalog = PricingCatalog()
    
    input_cost = 0.0
    cached_cost = 0.0
    output_cost = 0.0

    result = catalog.query(provider, "AmazonBedrock", region,
                          "Bedrock-Input-Token", uncached_input_tokens)
    if result and hasattr(result, 'total_cost'):
        input_cost = result.total_cost

    result = catalog.query(provider, "AmazonBedrock", region,
                          "Bedrock-Cached-Input-Token", cached_input_tokens)
    if result and hasattr(result, 'total_cost'):
        cached_cost = result.total_cost

    result = catalog.query(provider, "AmazonBedrock", region,
                          "Bedrock-Output-Token", output_tokens)
    if result and hasattr(result, 'total_cost'):
        output_cost = result.total_cost

    return input_cost + cached_cost + output_cost


def _model_cost_comparison(input_tokens: float, output_tokens: float, provider: str = "aws") -> dict:
    """Compare costs across LLM models.
    
    Note: Uses seed prices for comparison.
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
        input_result = catalog.query(provider, "AmazonBedrock", "us-east-1",
                                      "Bedrock-Input-Token", input_tokens)
        output_result = catalog.query(provider, "AmazonBedrock", "us-east-1",
                                      "Bedrock-Output-Token", output_tokens)
        
        total = 0.0
        if input_result and hasattr(input_result, 'total_cost'):
            total += input_result.total_cost
        if output_result and hasattr(output_result, 'total_cost'):
            total += output_result.total_cost
        
        results[model_name] = total
    
    return results


def is_economic_sink(node_type: str, provider: str) -> bool:
    """Check if a node is a leaf node (LLM node).
    
    LLM nodes accept input tokens and produce output tokens - both billable.
    """
    return provider in ("bedrock", "openai") and node_type == "compute"
