"""`validate` and `compute` agree on when a node needs provider and region (#273).

A catalog lookup needs the node's provider and region, and the engine never
guesses them (Principle 6, #164). A metric with a SaaS `shape` is priced by its
shape handler and never queries the catalog, so a node whose metrics all have a
shape needs neither field. `validate` reports the same nodes that `compute`
with a catalog rejects.
"""
import textwrap

import pytest

from infra_cost_model.cli import main
from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.catalog import PricingCatalog


def _model(node_yaml: str) -> str:
    return textwrap.dedent("""\
        version: "1.0"
        workflow:
          name: rule
          entry: svc
          frequency: {unit: perMonth, value: 1000}
        nodes:
          svc:
            resourceAddress: example.svc
        """) + textwrap.indent(textwrap.dedent(node_yaml), "    ") + "edges: []\n"


SHAPED_ONLY = """\
    nodeType: external
    service: saas
    usageMetrics:
      Seats: {unit: Seats, value: 3, fixed: true, shape: per_unit_flat, rate: 10.0}
"""

NO_PROVIDER = """\
    nodeType: compute
    service: lambda
    region: us-east-1
    usageMetrics:
      Requests: {unit: Requests, value: 1}
"""

NO_REGION = """\
    nodeType: compute
    provider: aws
    service: lambda
    usageMetrics:
      Requests: {unit: Requests, value: 1}
"""

MIXED = """\
    nodeType: external
    service: saas
    usageMetrics:
      Seats: {unit: Seats, value: 3, fixed: true, shape: per_unit_flat, rate: 10.0}
      Requests: {unit: Requests, value: 1}
"""

TIERED_SHAPED_ONLY = "    pricingModel: tiered\n" + SHAPED_ONLY


def _write(tmp_path, node_yaml):
    path = tmp_path / "m.yaml"
    path.write_text(_model(node_yaml))
    return path


def _compute_with_catalog(path):
    from infra_cost_model.sdk import parse_yaml_dsl
    model = parse_yaml_dsl(path.read_text())
    return CostEngine(model, catalog=PricingCatalog(), time_basis="monthly").compute()


@pytest.mark.parametrize("node_yaml", [SHAPED_ONLY, TIERED_SHAPED_ONLY])
def test_engine_prices_a_shaped_only_node_without_provider_or_region(tmp_path, node_yaml):
    costs = _compute_with_catalog(_write(tmp_path, node_yaml))
    assert costs["svc"] == pytest.approx(30.0)


@pytest.mark.parametrize("node_yaml,field", [
    (NO_PROVIDER, "provider"), (NO_REGION, "region"), (MIXED, "provider"),
])
def test_engine_rejects_a_node_that_queries_the_catalog(tmp_path, node_yaml, field):
    with pytest.raises(ValueError, match=f"missing required '{field}'"):
        _compute_with_catalog(_write(tmp_path, node_yaml))


@pytest.mark.parametrize("node_yaml,field", [
    (NO_PROVIDER, "provider"), (NO_REGION, "region"), (MIXED, "provider"),
])
def test_validate_reports_what_compute_rejects(tmp_path, capsys, node_yaml, field):
    path = _write(tmp_path, node_yaml)
    assert main(["validate", str(path)]) == 1
    out = capsys.readouterr().out
    assert "'svc'" in out and f"'{field}'" in out
    assert main(["compute", str(path)]) == 1


@pytest.mark.parametrize("node_yaml", [SHAPED_ONLY, TIERED_SHAPED_ONLY])
def test_validate_accepts_a_shaped_only_node(tmp_path, capsys, node_yaml):
    path = _write(tmp_path, node_yaml)
    assert main(["validate", str(path)]) == 0
    assert main(["compute", str(path)]) == 0


def test_validate_ignores_nodes_that_never_query_the_catalog(tmp_path):
    """A node with no usage metrics never reaches a catalog lookup."""
    path = _write(tmp_path, "    nodeType: routing\n")
    assert main(["validate", str(path)]) == 0
