"""Integration test for Stack: GitHub Copilot (usage-based billing).

Validates:
- The example model loads and passes schema validation
- The credit_pool shape prices subscription + overage above the included
  AI-credit allowance
- Per-seat org plans scale subscription and pooled allowance
- Symbolic parameter (creditsUsed) drives what-if analysis
"""

import pytest
import yaml
from pathlib import Path

from infra_cost_model.engine.engine import CostEngine, DAGValidator
from infra_cost_model.schema import validate_cost_model


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def load_yaml_model(name: str) -> dict:
    path = EXAMPLES_DIR / name
    with open(path) as f:
        return yaml.safe_load(f)


class TestCopilotModel:
    """Validate the GitHub Copilot model loads correctly."""

    def test_model_loads(self):
        model = load_yaml_model("github-copilot.yaml")
        assert model["version"] == "1.0"
        assert model["workflow"]["name"] == "github-copilot-business"

    def test_schema_valid(self):
        model = load_yaml_model("github-copilot.yaml")
        assert validate_cost_model(model) == []

    def test_entry_is_copilot_node(self):
        model = load_yaml_model("github-copilot.yaml")
        assert model["workflow"]["entry"] == "github_copilot.business"

    def test_credit_pool_shape_declared(self):
        model = load_yaml_model("github-copilot.yaml")
        credits = model["nodes"]["github_copilot.business"]["usageMetrics"]["credits"]
        assert credits["shape"] == "credit_pool"
        assert credits["subscription"] == 475.0
        assert credits["includedCredits"] == 47_500

    def test_credits_used_is_symbolic(self):
        model = load_yaml_model("github-copilot.yaml")
        credits = model["nodes"]["github_copilot.business"]["usageMetrics"]["credits"]
        assert credits["value"] == "creditsUsed"
        assert model["workflow"]["parameters"]["creditsUsed"] == 60_000

    def test_dag_no_cycles(self):
        model = load_yaml_model("github-copilot.yaml")
        validator = DAGValidator(model["nodes"], model["edges"])
        assert validator.validate() is True


class TestCopilotCost:
    """The example prices through the credit_pool shape."""

    @pytest.fixture
    def engine(self):
        model = load_yaml_model("github-copilot.yaml")
        return CostEngine(model, catalog=None, time_basis="monthly")

    def test_baseline_cost(self, engine):
        """60k credits used vs 47.5k included: $475 + 12.5k × $0.01 = $600."""
        costs = engine.compute()
        assert costs["github_copilot.business"] == pytest.approx(600.0)

    def test_within_pool(self):
        model = load_yaml_model("github-copilot.yaml")
        model["workflow"]["parameters"]["creditsUsed"] = 40_000
        engine = CostEngine(model, catalog=None, time_basis="monthly")
        costs = engine.compute()
        assert costs["github_copilot.business"] == pytest.approx(475.0)

    def test_what_if_credit_consumption(self):
        """Sweeping creditsUsed shows the overage cliff at 47,500 credits."""
        from infra_cost_model.engine import SensitivityAnalyzer
        analyzer = SensitivityAnalyzer(load_yaml_model("github-copilot.yaml"), catalog=None, time_basis="monthly")
        assert analyzer.what_if("creditsUsed", 47_500) == pytest.approx(475.0)
        assert analyzer.what_if("creditsUsed", 60_000) == pytest.approx(600.0)
        assert analyzer.what_if("creditsUsed", 100_000) == pytest.approx(1_000.0)
