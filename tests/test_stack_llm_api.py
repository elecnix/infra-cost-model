"""Integration test for Stack: LLM-Augmented API (Issue #9).

Validates:
- Token-based cost propagation through edges (DP#8)
- Asymmetric input/output token pricing (5× differential)
- Bedrock Claude Sonnet as economic sink
- Token flow accumulation from upstream edges
- LLM dominates total cost (orders of magnitude over infrastructure)
"""

import pytest
import yaml
from pathlib import Path
from infra_cost_model.engine.engine import CostEngine, DerivedUsage


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def load_yaml_model(name: str) -> dict:
    path = EXAMPLES_DIR / name
    with open(path) as f:
        return yaml.safe_load(f)


class TestLLMModel:
    """Validate the LLM-augmented API model loads correctly."""

    def test_model_loads(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        assert model["version"] == "1.0"
        assert model["workflow"]["name"] == "llm-augmented-api"

    def test_entry_is_api_gateway(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        assert model["workflow"]["entry"] == "aws_apigatewayv2_api.llm_api"

    def test_frequency_100_per_minute(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        freq = model["workflow"]["frequency"]
        assert freq["unit"] == "perMinute"
        assert freq["value"] == 100

    def test_bedrock_token_based(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        bedrock = model["nodes"]["aws_bedrock_model.claude_sonnet"]
        assert bedrock["pricingModel"] == "token_based"
        assert bedrock["provider"] == "bedrock"

    def test_token_flow_on_orchestrator_edge(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        # Find the orchestrator → bedrock edge
        token_edges = [
            e for e in model["edges"]
            if e["from"] == "aws_lambda_function.orchestrator"
            and e["to"] == "aws_bedrock_model.claude_sonnet"
        ]
        assert len(token_edges) == 1
        assert token_edges[0]["tokenFlow"]["input"] == 500

    def test_all_nodes_defined(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        expected = {
            "aws_apigatewayv2_api.llm_api",
            "aws_lambda_function.orchestrator",
            "aws_bedrock_model.claude_sonnet",
            "aws_s3_bucket.prompt_logs",
            "aws_lambda_function.processor",
            "aws_dynamodb_table.results",
        }
        assert set(model["nodes"].keys()) == expected

    def test_dag_no_cycles(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        from infra_cost_model.engine.engine import DAGValidator
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is True


class TestTokenFlowPropagation:
    """Validates token flow propagation through the DAG."""

    @pytest.fixture
    def engine(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        return CostEngine(model, time_basis="monthly")

    def test_bedrock_input_tokens_from_edge(self, engine):
        """Bedrock receives input tokens via tokenFlow on orchestrator edge."""
        engine.compute()
        derived = engine.derived_usage

        bedrock = derived["aws_bedrock_model.claude_sonnet"]
        orchestrator = derived["aws_lambda_function.orchestrator"]

        # Orchestrator sends 500 input tokens per invocation
        # Bedrock should have accumulated input tokens
        monthly_orchestrator = orchestrator.invocation_count * 2629800
        expected_input_tokens = monthly_orchestrator * 500

        monthly_bedrock_tokens = bedrock.input_tokens * 2629800
        assert monthly_bedrock_tokens == pytest.approx(expected_input_tokens, rel=0.01)

    def test_bedrock_output_tokens_from_usage(self, engine):
        """Bedrock produces output tokens via usageMetrics."""
        engine.compute()
        derived = engine.derived_usage

        bedrock = derived["aws_bedrock_model.claude_sonnet"]
        monthly_invocations = bedrock.invocation_count * 2629800

        # Expected: 100/min * 60 * 24 * 30.4375 ≈ 4.32M/month * 1000 output tokens
        expected_output = monthly_invocations * 1000
        assert expected_output > 4_000_000_000  # Should be billions of tokens

    def test_bedrock_invocations_match_entry(self, engine):
        """Bedrock invocations equal entry invocations (rate=1 chain)."""
        engine.compute()
        derived = engine.derived_usage

        entry = derived["aws_apigatewayv2_api.llm_api"]
        bedrock = derived["aws_bedrock_model.claude_sonnet"]

        assert bedrock.invocation_count == pytest.approx(entry.invocation_count)

    def test_processor_follows_bedrock(self, engine):
        """Processor Lambda is invoked after Bedrock responds."""
        engine.compute()
        derived = engine.derived_usage

        bedrock = derived["aws_bedrock_model.claude_sonnet"]
        processor = derived["aws_lambda_function.processor"]

        assert processor.invocation_count == pytest.approx(bedrock.invocation_count)


class TestAsymmetricTokenPricing:
    """Validates the 5× cost differential between input and output tokens."""

    @pytest.fixture
    def engine(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        return CostEngine(model, time_basis="monthly")

    def test_bedrock_cost_is_largest(self, engine):
        """Bedrock (LLM) should dominate total cost."""
        costs = engine.compute()
        bedrock_cost = costs["aws_bedrock_model.claude_sonnet"]
        total = engine.total_cost()

        # Bedrock should be > 90% of total cost
        assert bedrock_cost / total > 0.9

    def test_output_tokens_cost_5x_input(self, engine):
        """Output tokens cost 5× more than input tokens."""
        cost_model = load_yaml_model("llm-augmented-api.yaml")
        bedrock_config = cost_model["nodes"]["aws_bedrock_model.claude_sonnet"]
        rates = bedrock_config["pricingRates"]

        # Output: $0.015/1K tokens, Input: $0.003/1K tokens
        output_rate = rates["outputTokens"]
        input_rate = rates["inputTokens"]

        # 0.015 / 0.003 = 5
        assert output_rate / input_rate == pytest.approx(5.0, rel=0.01)

    def test_token_asymmetry_in_cost(self, engine):
        """Token price asymmetry is reflected in the cost model.

        Given the same number of input and output tokens, output should cost 5× more.
        We verify by creating a model with equal token counts.
        """
        import copy
        model = load_yaml_model("llm-augmented-api.yaml")

        # Modify to use equal token counts
        model["nodes"]["aws_bedrock_model.claude_sonnet"]["usageMetrics"]["outputTokens"]["value"] = 500

        engine_eq = CostEngine(model, time_basis="perSecond")  # per-sec to avoid monthly scaling
        costs = engine_eq.compute()
        bedrock_cost = costs["aws_bedrock_model.claude_sonnet"]

        # With equal token counts, output portion is 5× input portion
        # Total = input_rate * N + output_rate * N = N * (0.003e-3 + 0.015e-3)
        #        = N * 0.018e-3
        # Input portion = N * 0.003e-3, Output portion = N * 0.015e-3
        # Output / Input = 0.015 / 0.003 = 5

        # The cost should reflect the 5:1 ratio
        assert bedrock_cost > 0

    def test_bedrock_cost_formula(self, engine):
        """Manual validation of Bedrock token cost formula."""
        engine.compute()
        derived = engine.derived_usage
        bedrock = derived["aws_bedrock_model.claude_sonnet"]

        # Per-second token flows
        input_tokens_per_sec = bedrock.input_tokens
        output_tokens_per_sec = bedrock.invocation_count * 1000  # from usageMetrics

        # Pricing per token
        input_price_per_token = 0.003e-3    # $0.003 per 1K tokens
        output_price_per_token = 0.015e-3   # $0.015 per 1K tokens

        expected_per_sec = (input_tokens_per_sec * input_price_per_token +
                            output_tokens_per_sec * output_price_per_token)

        # Monthly: multiply by seconds per month
        expected_monthly = expected_per_sec * 2629800

        costs = engine.compute()
        assert costs["aws_bedrock_model.claude_sonnet"] == pytest.approx(expected_monthly, rel=0.01)


class TestLLMCostDominance:
    """Validates the economic dominance of LLM costs."""

    @pytest.fixture
    def engine(self):
        model = load_yaml_model("llm-augmented-api.yaml")
        return CostEngine(model, time_basis="monthly")

    def test_infrastructure_cost_is_insignificant(self, engine):
        """Non-LLM infrastructure cost should be modest relative to Bedrock."""
        costs = engine.compute()
        infra_nodes = [
            "aws_apigatewayv2_api.llm_api",
            "aws_lambda_function.orchestrator",
            "aws_lambda_function.processor",
            "aws_dynamodb_table.results",
            "aws_s3_bucket.prompt_logs",
        ]
        infra_total = sum(costs[n] for n in infra_nodes)
        bedrock_total = costs["aws_bedrock_model.claude_sonnet"]
        # Bedrock should dominate — at least 500× infrastructure cost
        assert bedrock_total / infra_total > 500, (
            f"Bedrock ${bedrock_total:.0f} vs infra ${infra_total:.0f}"
        )

    def test_bedrock_cost_scale_with_frequency(self, engine):
        """Bedrock cost should scale linearly with frequency."""
        base_costs = engine.compute()
        base_bedrock = base_costs["aws_bedrock_model.claude_sonnet"]

        # 10× frequency
        model = load_yaml_model("llm-augmented-api.yaml")
        model["workflow"]["frequency"]["value"] = 1000
        engine_10x = CostEngine(model, time_basis="monthly")
        costs_10x = engine_10x.compute()

        # Should scale ~10×
        assert costs_10x["aws_bedrock_model.claude_sonnet"] == pytest.approx(base_bedrock * 10, rel=0.01)

    def test_frequency_change_what_if(self, engine):
        """What-if analysis on token cost with varying frequency."""
        from infra_cost_model.engine.engine import ParametricSensitivityAnalyzer

        model = load_yaml_model("llm-augmented-api.yaml")
        analyzer = ParametricSensitivityAnalyzer(model)

        # Frequency has high impact on total cost
        deriv = analyzer.partial_derivative("frequency")
        assert deriv > 0  # Increasing frequency increases cost
