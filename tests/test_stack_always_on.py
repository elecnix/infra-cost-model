"""Integration test for Stack: Always-on / fixed infrastructure (Issue #196).

Validates:
- Per-metric `fixed` flags: one node carries both a fixed monthly dimension and
  a usage-driven dimension (NAT gateway, ALB).
- Always-on nodes are costed without a synthetic incoming edge and emit no
  unreachable warning (Secrets Manager secret).
- A mixed fixed/usage node may receive DAG edges with no DP#9 conflict warning;
  the warning fires only for a fully-fixed node that also receives edges.
"""

import warnings
from pathlib import Path

import pytest
import yaml

from infra_cost_model.engine.engine import CostEngine, SECONDS_PER_MONTH


EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def load_model() -> dict:
    with open(EXAMPLES_DIR / "always-on-infrastructure.yaml") as f:
        return yaml.safe_load(f)


class TestAlwaysOnModel:
    def test_model_loads(self):
        model = load_model()
        assert model["version"] == "1.0"
        assert model["workflow"]["entry"] == "aws_lb.main"

    def test_secret_is_always_on(self):
        """The Secrets Manager secret has no incoming edge yet is marked fixed."""
        model = load_model()
        secret = model["nodes"]["aws_secretsmanager_secret.api_key"]
        assert secret["usageMetrics"]["secretMonths"]["fixed"] is True
        targets = {e["to"] for e in model["edges"]}
        assert "aws_secretsmanager_secret.api_key" not in targets

    def test_nat_is_mixed(self):
        """The NAT gateway carries one fixed and one usage-driven metric."""
        model = load_model()
        nat = model["nodes"]["aws_nat_gateway.main"]["usageMetrics"]
        assert nat["gatewayHours"]["fixed"] is True
        assert "fixed" not in nat["gbProcessed"]


class TestAlwaysOnEngine:
    def test_all_nodes_costed_including_edgeless_secret(self):
        """Every node — including the edgeless always-on secret — is costed."""
        model = load_model()
        costs = CostEngine(model, time_basis="monthly").compute()
        for addr in model["nodes"]:
            assert addr in costs, f"Node '{addr}' missing from costs"

    def test_secret_costed_without_synthetic_edge(self):
        """The always-on secret is costed at its flat monthly total."""
        model = load_model()
        costs = CostEngine(model, time_basis="monthly").compute()
        # secretMonths = 1 × $0.40 — flat, independent of any flow.
        assert costs["aws_secretsmanager_secret.api_key"] == pytest.approx(0.40)

    def test_no_unreachable_warning_for_always_on_nodes(self):
        """No unreachable warning despite the secret having no incoming edge."""
        model = load_model()
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            CostEngine(model, time_basis="monthly").compute()
        unreachable = [w for w in record if "unreachable" in str(w.message).lower()]
        assert unreachable == []

    def test_no_dp9_warning_for_mixed_nat_node(self):
        """The mixed NAT node receives an edge but raises no DP#9 conflict."""
        model = load_model()
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("always")
            CostEngine(model, time_basis="monthly").compute()
        conflicts = [w for w in record if "escape" in str(w.message).lower()]
        assert conflicts == []

    def test_fixed_part_independent_of_frequency(self):
        """The fixed dimensions don't change when entry frequency changes."""
        model = load_model()
        base = CostEngine(model, time_basis="monthly").compute()

        model_10x = load_model()
        model_10x["workflow"]["frequency"]["value"] *= 10
        scaled = CostEngine(model_10x, time_basis="monthly").compute()

        # Secret is purely fixed → unchanged.
        assert scaled["aws_secretsmanager_secret.api_key"] == pytest.approx(
            base["aws_secretsmanager_secret.api_key"]
        )
        # NAT is mixed → its cost grows, but by less than 10× (fixed hours stay).
        assert scaled["aws_nat_gateway.main"] > base["aws_nat_gateway.main"]
        assert scaled["aws_nat_gateway.main"] < base["aws_nat_gateway.main"] * 10

    def test_nat_fixed_floor(self):
        """NAT cost is at least its fixed gateway-hours total."""
        model = load_model()
        costs = CostEngine(model, time_basis="monthly").compute()
        fixed_floor = 730 * 0.045
        assert costs["aws_nat_gateway.main"] > fixed_floor

    def test_total_cost_positive(self):
        model = load_model()
        assert CostEngine(model, time_basis="monthly").total_cost() > 0
