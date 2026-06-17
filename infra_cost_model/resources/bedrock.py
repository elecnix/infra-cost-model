"""Bedrock/LLM Model resource model implementation."""

from typing import Optional
from .types import ComputeResource, ResourceExtract


# Model pricing rates (per 1K tokens)
MODEL_RATES = {
    "claude-3-5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-5-haiku": {"input": 0.001, "output": 0.005},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "gpt-4o": {"input": 0.0025, "output": 0.010},
    "gpt-4-turbo": {"input": 0.010, "output": 0.030},
}


class BedrockModel(ComputeResource):
    """Bedrock LLM model - compute node with token-based costing.
    
    LLM nodes are economic sinks where input tokens flow in and output tokens flow out,
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


def bedrock_cost(input_tokens: float, output_tokens: float, model: str = "claude-3-5-sonnet",
                 catalog=None) -> float:
    """Calculate Bedrock/LLM model cost."""
    return _bedrock_token_cost(input_tokens, 0.0, output_tokens, model, catalog)


def cached_prompt_bedrock_cost(input_tokens: float, cached_input_tokens: float,
                               output_tokens: float,
                               model: str = "claude-3-5-sonnet",
                               catalog=None) -> float:
    """Calculate Bedrock cost with cached prompt input discounted at 50%."""
    cached_input_tokens = min(max(cached_input_tokens, 0), input_tokens)
    uncached_input_tokens = input_tokens - cached_input_tokens
    return _bedrock_token_cost(uncached_input_tokens, cached_input_tokens, output_tokens, model, catalog)


def streaming_bedrock_cost(input_tokens: float, output_tokens: float,
                           model: str = "claude-3-5-sonnet",
                           catalog=None) -> float:
    """Streaming delivery does not change total token cost."""
    return bedrock_cost(input_tokens, output_tokens, model, catalog)


def _bedrock_token_cost(uncached_input_tokens: float, cached_input_tokens: float,
                        output_tokens: float, model: str, catalog=None) -> float:
    rates = MODEL_RATES.get(model, MODEL_RATES["claude-3-5-sonnet"])

    if catalog:
        input_cost = 0.0
        cached_cost = 0.0
        output_cost = 0.0

        result = catalog.query("aws", "AmazonBedrock", "us-east-1",
                              "Bedrock-Input-Token", uncached_input_tokens)
        if result and hasattr(result, 'total_cost'):
            input_cost = result.total_cost

        result = catalog.query("aws", "AmazonBedrock", "us-east-1",
                              "Bedrock-Cached-Input-Token", cached_input_tokens)
        if result and hasattr(result, 'total_cost'):
            cached_cost = result.total_cost
        elif cached_input_tokens:
            cached_cost = cached_input_tokens * rates["input"] * 0.5 / 1000

        result = catalog.query("aws", "AmazonBedrock", "us-east-1",
                              "Bedrock-Output-Token", output_tokens)
        if result and hasattr(result, 'total_cost'):
            output_cost = result.total_cost

        return input_cost + cached_cost + output_cost

    input_cost = uncached_input_tokens * rates["input"] / 1000
    cached_cost = cached_input_tokens * rates["input"] * 0.5 / 1000
    output_cost = output_tokens * rates["output"] / 1000

    return input_cost + cached_cost + output_cost


def model_cost_comparison(input_tokens: float, output_tokens: float) -> dict:
    """Compare costs across LLM models."""
    results = {}
    
    for model_name, rates in MODEL_RATES.items():
        input_cost = input_tokens * rates["input"] / 1000
        output_cost = output_tokens * rates["output"] / 1000
        results[model_name] = input_cost + output_cost
    
    return results


def is_economic_sink(node_type: str, provider: str) -> bool:
    """Check if a node is an economic sink (LLM node).
    
    LLM nodes accept input tokens and produce output tokens - both billable.
    """
    return provider in ("bedrock", "openai") and node_type == "compute"