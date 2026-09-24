"""Tests for Bedrock/LLM Model resource model."""

import pytest
from infra_cost_model.resources.bedrock import (
    BedrockModel, _bedrock_cost, _cached_prompt_bedrock_cost,
    _streaming_bedrock_cost, _model_cost_comparison
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


def test_bedrock_cost_calculation(seed_catalog):
    """Test Bedrock cost calculation using catalog prices."""
    # Using seed prices: input $0.003/1K, output $0.015/1K
    # 2.16B input + 4.32B output
    cost = _bedrock_cost(2_160_000_000, 4_320_000_000, "claude-3-5-sonnet",
                         catalog=seed_catalog, region="us-east-1")

    expected = 2_160_000_000 * 0.003 / 1000 + 4_320_000_000 * 0.015 / 1000
    # = 6480 + 64800 = $71,280

    assert cost > 0
    assert cost == pytest.approx(expected, rel=0.01)


def test_bedrock_model_switching():
    """Test cost changes when switching models (all use same seed prices)."""
    input_tokens = 1_000_000_000
    output_tokens = 2_000_000_000

    sonnet_cost = _bedrock_cost(input_tokens, output_tokens, "claude-3-5-sonnet", region="us-east-1")
    haiku_cost = _bedrock_cost(input_tokens, output_tokens, "claude-3-5-haiku", region="us-east-1")

    # All models currently use the same seed prices, so costs are equal
    # In a real implementation, models would have different pricing
    assert sonnet_cost == haiku_cost


def test_bedrock_asymmetric_pricing(seed_catalog):
    """Test that output tokens are more expensive than input."""
    # For Claude Sonnet: output is 5x input
    input_tokens = 1_000_000
    output_tokens = 1_000_000

    cost = _bedrock_cost(input_tokens, output_tokens, "claude-3-5-sonnet",
                         catalog=seed_catalog, region="us-east-1")
    input_cost = input_tokens * 0.003 / 1000
    output_cost = output_tokens * 0.015 / 1000

    assert output_cost == 5 * input_cost
    assert cost == pytest.approx(input_cost + output_cost)


def test_model_cost_comparison():
    """Test comparing costs across models."""
    results = _model_cost_comparison(1_000_000, 2_000_000, region="us-east-1")

    assert "claude-3-5-sonnet" in results
    assert "claude-3-5-haiku" in results
    assert "claude-3-opus" in results

    # All models currently use the same seed prices
    assert results["claude-3-5-haiku"] == results["claude-3-5-sonnet"]


def test_cached_prompt_cost_discount(seed_catalog):
    """Test prompt caching discount for cached input tokens."""
    cost = _cached_prompt_bedrock_cost(
        input_tokens=1_000_000,
        cached_input_tokens=500_000,
        output_tokens=1_000_000,
        model="claude-3-5-sonnet",
        catalog=seed_catalog,
        region="us-east-1",
    )

    expected = (
        500_000 * 0.003 / 1000
        + 500_000 * 0.003 * 0.5 / 1000
        + 1_000_000 * 0.015 / 1000
    )

    assert cost == pytest.approx(expected, rel=0.01)


def test_streaming_cost_matches_total_tokens():
    """Streaming changes delivery, not total token cost."""
    assert _streaming_bedrock_cost(1_000_000, 2_000_000, region="us-east-1") == _bedrock_cost(1_000_000, 2_000_000, region="us-east-1")


def test_bedrock_handler_owns_the_terraform_style_address():
    """The LLM example names its node `aws_bedrock_model.<name>` (#312)."""
    assert BedrockModel.from_address("aws_bedrock_model.claude_sonnet") is not None


def test_bedrock_maps_token_metrics_to_catalog_rows():
    """Logical token names map to the AmazonBedrock seed rows (#312)."""
    assert BedrockModel().catalog_metrics == {
        "inputTokens": "Bedrock-Input-Token",
        "cachedReadTokens": "Bedrock-Cached-Input-Token",
        "outputTokens": "Bedrock-Output-Token",
    }


def test_token_based_bedrock_node_prices_from_the_seed_catalog(seed_catalog):
    """A Bedrock node without pricingRates prices every token class from the
    seed rows: $3/M input, $1.5/M cached input, $15/M output (#312)."""
    from infra_cost_model.engine import CostEngine

    model = {
        "version": "1.0",
        "workflow": {"name": "llm", "entry": "aws_bedrock_model.claude",
                     "frequency": {"unit": "perMonth", "value": 1000}},
        "nodes": {
            "aws_bedrock_model.claude": {
                "nodeType": "compute",
                "resourceAddress": "aws_bedrock_model.claude",
                "provider": "aws",
                "service": "AmazonBedrock",
                "region": "us-east-1",
                "pricingModel": "token_based",
                "usageMetrics": {
                    "inputTokens": {"unit": "tokens", "value": 500},
                    "cachedReadTokens": {"unit": "tokens", "value": 200},
                    "outputTokens": {"unit": "tokens", "value": 1000},
                },
            },
        },
        "edges": [],
    }
    engine = CostEngine(model, catalog=seed_catalog, time_basis="monthly")
    cost = engine.compute()["aws_bedrock_model.claude"]

    expected = 1000 * (500 * 3 + 200 * 1.5 + 1000 * 15) / 1_000_000
    assert cost == pytest.approx(expected)
    assert engine.unpriced_metrics == []


@pytest.mark.parametrize("extract, resource", [
    (BedrockModel.extract_tf, {"address": "aws_bedrock_model.claude",
                               "values": {"model_id": "anthropic.claude-3-5-sonnet",
                                          "region": "us-east-1"}}),
    (BedrockModel.extract_pulumi, {"id": "aws.bedrock.Model:claude",
                                   "inputs": {"modelId": "anthropic.claude-3-5-sonnet",
                                              "region": "us-east-1"}}),
])
def test_extracted_bedrock_node_matches_the_seed_rows(extract, resource):
    """Imported Bedrock nodes use the vendor and service of the seed rows (#320)."""
    result = extract(resource)

    assert (result.provider, result.service, result.region) == \
        ("aws", "AmazonBedrock", "us-east-1")
    assert result.config == {"modelId": "anthropic.claude-3-5-sonnet"}


def test_extracted_cdk_bedrock_node_matches_the_seed_rows():
    """CDK templates carry no region, so the CDK extract leaves it unset (#320)."""
    result = BedrockModel.extract_cdk({"LogicalId": "Claude",
                                       "Properties": {"ModelId": "anthropic.claude-3-5-sonnet"}})

    assert (result.provider, result.service, result.region) == ("aws", "AmazonBedrock", None)
    assert result.config == {"modelId": "anthropic.claude-3-5-sonnet"}


def test_bedrock_node_from_a_terraform_plan_prices_from_the_seed_catalog(seed_catalog):
    """A Bedrock resource imported from a Terraform plan finds its seed prices (#320)."""
    from infra_cost_model.engine import CostEngine

    extract = BedrockModel.extract_tf({
        "address": "aws_bedrock_model.claude",
        "type": "aws_bedrock_model",
        "values": {"model_id": "anthropic.claude-3-5-sonnet", "region": "us-east-1"},
    })
    model = {
        "version": "1.0",
        "workflow": {"name": "llm", "entry": extract.resource_address,
                     "frequency": {"unit": "perMonth", "value": 1000}},
        "nodes": {
            extract.resource_address: {
                "nodeType": extract.node_type,
                "resourceAddress": extract.resource_address,
                "provider": extract.provider,
                "service": extract.service,
                "region": extract.region,
                "pricingModel": "token_based",
                "usageMetrics": {
                    "inputTokens": {"unit": "tokens", "value": 500},
                    "outputTokens": {"unit": "tokens", "value": 1000},
                },
            },
        },
        "edges": [],
    }
    engine = CostEngine(model, catalog=seed_catalog, time_basis="monthly")
    cost = engine.compute()[extract.resource_address]

    assert cost == pytest.approx(1000 * (500 * 3 + 1000 * 15) / 1_000_000)
    assert engine.unpriced_metrics == []
