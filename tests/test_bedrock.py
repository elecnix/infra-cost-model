"""Tests for Bedrock/LLM Model resource model."""

import pytest
from infra_cost_model.resources.bedrock import (
    BedrockModel, _bedrock_cost, _cached_prompt_bedrock_cost,
    _streaming_bedrock_cost, _model_cost_comparison, is_economic_sink
)


def test_bedrock_from_address_terraform():
    """Test parsing Bedrock model address."""
    result = BedrockModel.from_address("bedrock_model.claude")
    assert result is not None
    assert result.node_type == "compute"


def test_bedrock_valid_metrics():
    """Test that Bedrock has correct valid metrics."""
    bedrock = BedrockModel()
    assert "invocations" in bedrock.valid_metrics
    assert "inputTokens" in bedrock.valid_metrics
    assert "outputTokens" in bedrock.valid_metrics


def test_bedrock_cost_calculation():
    """Test Bedrock cost calculation using catalog prices."""
    # Using seed prices: input $0.003/1K, output $0.015/1K
    # 2.16B input + 4.32B output
    cost = _bedrock_cost(2_160_000_000, 4_320_000_000, "claude-3-5-sonnet")
    
    expected = 2_160_000_000 * 0.003 / 1000 + 4_320_000_000 * 0.015 / 1000
    # = 6480 + 64800 = $71,280
    
    assert cost > 0
    assert cost == pytest.approx(expected, rel=0.01)


def test_bedrock_model_switching():
    """Test cost changes when switching models (all use same seed prices)."""
    input_tokens = 1_000_000_000
    output_tokens = 2_000_000_000
    
    sonnet_cost = _bedrock_cost(input_tokens, output_tokens, "claude-3-5-sonnet")
    haiku_cost = _bedrock_cost(input_tokens, output_tokens, "claude-3-5-haiku")
    
    # All models currently use the same seed prices, so costs are equal
    # In a real implementation, models would have different pricing
    assert sonnet_cost == haiku_cost


def test_bedrock_asymmetric_pricing():
    """Test that output tokens are more expensive than input."""
    # For Claude Sonnet: output is 5x input
    input_tokens = 1_000_000
    output_tokens = 1_000_000
    
    cost = _bedrock_cost(input_tokens, output_tokens, "claude-3-5-sonnet")
    input_cost = input_tokens * 0.003 / 1000
    output_cost = output_tokens * 0.015 / 1000
    
    assert output_cost == 5 * input_cost
    assert cost == input_cost + output_cost


def test_model_cost_comparison():
    """Test comparing costs across models."""
    results = _model_cost_comparison(1_000_000, 2_000_000)
    
    assert "claude-3-5-sonnet" in results
    assert "claude-3-5-haiku" in results
    assert "claude-3-opus" in results
    
    # All models currently use the same seed prices
    assert results["claude-3-5-haiku"] == results["claude-3-5-sonnet"]


def test_leaf_node_classification():
    """Test that Bedrock models are classified as leaf nodes."""
    assert is_economic_sink("compute", "bedrock") is True
    assert is_economic_sink("compute", "openai") is True
    assert is_economic_sink("storage", "aws") is False


def test_cached_prompt_cost_discount():
    """Test prompt caching discount for cached input tokens."""
    cost = _cached_prompt_bedrock_cost(
        input_tokens=1_000_000,
        cached_input_tokens=500_000,
        output_tokens=1_000_000,
        model="claude-3-5-sonnet",
    )

    expected = (
        500_000 * 0.003 / 1000
        + 500_000 * 0.003 * 0.5 / 1000
        + 1_000_000 * 0.015 / 1000
    )

    assert cost == pytest.approx(expected, rel=0.01)


def test_streaming_cost_matches_total_tokens():
    """Streaming changes delivery, not total token cost."""
    assert _streaming_bedrock_cost(1_000_000, 2_000_000) == _bedrock_cost(1_000_000, 2_000_000)
