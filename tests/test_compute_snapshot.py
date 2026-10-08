"""`compute --format json` prints a diffable cost snapshot (#443).

The snapshot exists so a user can commit it and a reviewer can see which node
moved. That means the numbers must be stable: keys sorted, money rounded, and
every metric priced with its quantity, unit price and price source, so two
machines that disagree say why.
"""

import json
import re
import warnings
from pathlib import Path

import pytest

from infra_cost_model import __version__, cli
from infra_cost_model.cli import main


# A number that carries more precision than a snapshot should show: more
# than six decimal places, or an exponent of three or more digits (a short
# exponent like 5e-07 is a legitimate unit price, not a leaked float).
FULL_PRECISION = re.compile(r"\d\.\d{7,}|\d[eE][-+]?\d{3,}")

MODEL = """
version: "1.0"
workflow:
  name: snapshot-test
  entry: gateway
  frequency:
    unit: perMinute
    value: 600
nodes:
  gateway:
    nodeType: routing
    resourceAddress: gateway
    usageMetrics:
      hours: { unit: hours, value: 730, fixed: true }
      sessions: { unit: sessions, value: 2 }
    pricingRates:
      hours: 0.05
      sessions: 0.01
  worker:
    nodeType: compute
    resourceAddress: worker
    usageMetrics:
      invocations: { unit: requests, value: 1 }
    pricingRates:
      invocations: 0.0000005
edges:
  - { from: gateway, to: worker, rate: 1.0 }
"""

UNPRICED_MODEL = """
version: "1.0"
workflow:
  name: snapshot-unpriced
  entry: table
  frequency:
    unit: perMinute
    value: 600
nodes:
  table:
    nodeType: storage
    resourceAddress: table
    usageMetrics:
      reads: { unit: requests, value: 1 }
edges: []
"""


@pytest.fixture
def model_file(tmp_path) -> Path:
    path = tmp_path / "model.yaml"
    path.write_text(MODEL)
    return path


def snapshot(model: Path, capsys, *extra: str,
             catalog: bool = False) -> tuple[dict, str]:
    """Run `compute --format json` and return the parsed snapshot and its text."""
    if not catalog:
        extra = ("--no-catalog",) + extra
    exit_code = main(["compute", str(model), "--format", "json", *extra])
    out = capsys.readouterr().out
    assert exit_code == 0, out
    return json.loads(out), out


def test_snapshot_states_its_schema_and_engine(model_file, capsys):
    report, _ = snapshot(model_file, capsys, "--time-basis", "monthly")
    assert report["schemaVersion"] == 1
    assert report["engineVersion"] == __version__
    assert report["timeBasis"] == "monthly"


def test_snapshot_reports_the_totals_the_table_reports(model_file, capsys):
    report, _ = snapshot(model_file, capsys, "--time-basis", "monthly")
    assert report["total"] == pytest.approx(526009.649, abs=1e-6)
    assert report["nodes"]["gateway"]["total"] == pytest.approx(525996.5, abs=1e-6)
    assert report["nodes"]["worker"]["total"] == pytest.approx(13.149, abs=1e-6)


def test_snapshot_lists_every_metric_with_its_quantity_and_price(model_file, capsys):
    report, _ = snapshot(model_file, capsys, "--time-basis", "monthly")
    metrics = report["nodes"]["gateway"]["metrics"]
    assert set(metrics) == {"hours", "sessions"}
    # A fixed metric holds its value as a monthly total.
    assert metrics["hours"]["quantity"] == pytest.approx(730)
    assert metrics["hours"]["unitPrice"] == pytest.approx(0.05)
    assert metrics["hours"]["cost"] == pytest.approx(36.5, abs=1e-6)
    assert metrics["hours"]["fixed"] is True
    # A usage-driven metric covers a month of derived usage.
    assert metrics["sessions"]["quantity"] == pytest.approx(2 * 10 * 2629800, rel=1e-6)
    assert metrics["sessions"]["fixed"] is False


def test_snapshot_splits_fixed_and_variable_cost(model_file, capsys):
    report, _ = snapshot(model_file, capsys, "--time-basis", "monthly")
    gateway = report["nodes"]["gateway"]
    assert gateway["fixed"] == pytest.approx(36.5, abs=1e-6)
    assert gateway["variable"] == pytest.approx(525960.0, abs=1e-6)
    assert gateway["total"] == pytest.approx(gateway["fixed"] + gateway["variable"],
                                             abs=1e-6)


def test_every_node_adds_up_to_the_total(model_file, capsys):
    report, _ = snapshot(model_file, capsys, "--time-basis", "monthly")
    for node in report["nodes"].values():
        metrics = sum(m["cost"] for m in node["metrics"].values())
        assert metrics == pytest.approx(node["total"], abs=1e-6), node
    assert sum(n["total"] for n in report["nodes"].values()) == pytest.approx(
        report["total"], abs=1e-6)


def test_printed_numbers_add_up_as_printed(tmp_path, capsys, seed_catalog,
                                           monkeypatch):
    """A node's printed metrics must not drift from its printed total.

    Rounding each field on its own can leave a node's printed metrics a
    millionth away from its printed total (a Lambda's GB-seconds plus its
    requests does it). The snapshot keeps the engine's own numbers, so the
    same node in two snapshots carries the same total as the table printed.
    """
    monkeypatch.setattr(cli, "PricingCatalog", lambda *a, **k: seed_catalog)
    path = tmp_path / "pipeline.yaml"
    path.write_text(Path("examples/data-pipeline.yaml").read_text())
    table = main(["compute", str(path), "--time-basis", "monthly"])
    table_out = capsys.readouterr().out
    assert table == 0, table_out
    table_total = float(table_out.rsplit("$", 1)[1])

    report, _ = snapshot(path, capsys, "--time-basis", "monthly", catalog=True)
    assert report["total"] == pytest.approx(table_total, abs=1e-9)
    for node in report["nodes"].values():
        assert node["fixed"] + node["variable"] == pytest.approx(
            node["total"], abs=1e-9)
        assert sum(m["cost"] for m in node["metrics"].values()) == pytest.approx(
            node["total"], abs=1e-6), node
    assert sum(n["total"] for n in report["nodes"].values()) == pytest.approx(
        report["total"], abs=1e-6)


def test_snapshot_says_where_each_price_came_from(model_file, capsys):
    report, _ = snapshot(model_file, capsys, "--time-basis", "monthly")
    sources = {
        metric["priceSource"]
        for node in report["nodes"].values()
        for metric in node["metrics"].values()
    }
    # This model's metrics price from its own embedded rates.
    assert sources == {"pricingRates"}


def test_snapshot_reports_a_catalog_price_source(tmp_path, capsys, seed_catalog,
                                                 monkeypatch):
    """A price from the catalog says which catalog it came from."""
    monkeypatch.setattr(cli, "PricingCatalog", lambda *a, **k: seed_catalog)
    path = tmp_path / "always-on.yaml"
    path.write_text(Path("examples/always-on-infrastructure.yaml").read_text())
    exit_code = main([
        "compute", str(path), "--format", "json", "--time-basis", "monthly",
    ])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    nat = report["nodes"]["aws_nat_gateway.main"]["metrics"]["natHours"]
    assert nat["priceSource"] in {"seed", "vendor", "infracost", "azure-retail"}
    assert nat["fixed"] is True


def test_snapshot_lists_the_metrics_it_could_not_price(tmp_path, capsys):
    path = tmp_path / "unpriced.yaml"
    path.write_text(UNPRICED_MODEL)
    report, _ = snapshot(path, capsys, "--time-basis", "monthly")
    assert [m["metric"] for m in report["unpriced"]] == ["reads"]
    assert report["unpriced"][0]["node"] == "table"
    assert report["nodes"]["table"]["metrics"] == {}
    assert report["total"] == 0.0


def test_snapshot_output_is_sorted_and_rereadable(model_file, capsys):
    report, out = snapshot(model_file, capsys, "--time-basis", "monthly")
    assert out == json.dumps(report, indent=2, sort_keys=True) + "\n"
    assert list(report["nodes"]) == sorted(report["nodes"])


def test_snapshot_never_prints_full_precision_floats(model_file, capsys):
    _, out = snapshot(model_file, capsys, "--time-basis", "monthly")
    assert not FULL_PRECISION.search(out), out


def test_snapshot_keeps_per_second_costs_above_zero(model_file, capsys):
    """A per-second cost is far below a cent, and must not round away to 0."""
    report, _ = snapshot(model_file, capsys)
    assert report["timeBasis"] == "perSecond"
    assert report["total"] > 0
    assert report["nodes"]["worker"]["total"] > 0


def test_snapshot_output_is_identical_across_runs(model_file, capsys):
    first, first_out = snapshot(model_file, capsys, "--time-basis", "monthly")
    second, second_out = snapshot(model_file, capsys, "--time-basis", "monthly")
    assert first_out == second_out
    assert first == second


def test_table_is_still_the_default_format(model_file, capsys):
    assert main(["compute", str(model_file), "--no-catalog"]) == 0
    out = capsys.readouterr().out
    assert "Total Monthly Cost" in out or "Total Per-Second Cost" in out
    assert not out.startswith("{")


def test_snapshot_module_builds_the_same_report(model_file):
    """The snapshot is a function of the engine, not only of the CLI."""
    from infra_cost_model.engine import CostEngine
    from infra_cost_model.engine.snapshot import build_snapshot
    from infra_cost_model.sdk import parse_yaml_dsl

    model = parse_yaml_dsl(model_file.read_text())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        engine = CostEngine(model, catalog=None, time_basis="monthly")
        engine.compute()
        report = build_snapshot(engine)
    assert report["schemaVersion"] == 1
    assert report["nodes"]["worker"]["total"] == pytest.approx(13.149, abs=1e-6)


POOLED_MODEL = """
version: "1.0"
workflow:
  name: snapshot-pooled
  entry: data_transfer.a
  frequency:
    unit: perMonth
    value: 1
nodes:
  data_transfer.a:
    nodeType: external
    provider: aws
    service: AWSDataTransfer
    region: us-east-1
    usageMetrics:
      internetOutGb: { unit: GB, value: 50 }
  data_transfer.b:
    nodeType: external
    provider: aws
    service: AWSDataTransfer
    region: us-east-1
    usageMetrics:
      internetOutGb: { unit: GB, value: 150 }
  data_transfer.c:
    nodeType: external
    provider: aws
    service: AWSDataTransfer
    region: us-east-1
    usageMetrics:
      internetOutGb: { unit: GB, value: 100 }
edges:
  - { from: data_transfer.a, to: data_transfer.b, rate: 1.0 }
  - { from: data_transfer.b, to: data_transfer.c, rate: 1.0 }
"""


@pytest.mark.parametrize("basis", ["perSecond", "monthly", "yearly"])
def test_pooled_price_is_charged_to_the_metric_that_incurred_it(
        tmp_path, capsys, seed_catalog, monkeypatch, basis):
    """A price shared between nodes is still charged to the right usage metric.

    Three egress nodes share one free allowance and one set of rate tiers
    (#294). The engine reprices their combined 300 GB, and the snapshot must
    charge that pooled price back to each node's own metric, so a node's
    metrics still add up to its cost in every time basis.
    """
    monkeypatch.setattr(cli, "PricingCatalog", lambda *a, **k: seed_catalog)
    path = tmp_path / "pooled.yaml"
    path.write_text(POOLED_MODEL)
    report, _ = snapshot(path, capsys, "--time-basis", basis, catalog=True)

    nodes = report["nodes"]
    assert sum(n["total"] for n in nodes.values()) == pytest.approx(
        report["total"], abs=1e-5)
    for address, node in nodes.items():
        assert set(node["metrics"]) == {"internetOutGb"}, address
        metric = node["metrics"]["internetOutGb"]
        # This model's only metric is usage-driven, so the whole cost of the
        # node is on it.
        assert metric["cost"] == pytest.approx(node["total"], abs=1e-5)
        assert node["fixed"] == 0.0
        assert metric["fixed"] is False
        assert metric["priceSource"] in {"seed", "vendor", "infracost"}
    # The pool splits its price by quantity, so every node pays the same
    # effective rate, and the three shares add up to the pool's price.
    rates = {node["metrics"]["internetOutGb"]["unitPrice"]
             for node in nodes.values()}
    assert len(rates) == 1
    assert 0 < report["total"]

    # The first node's own 50 GB alone sits inside the free allowance and
    # costs nothing (#294). Sharing the allowance with the other two makes it
    # pay, and the snapshot puts that pooled price on its own metric.
    assert _solo_cost(seed_catalog, 50, basis) == 0.0
    assert nodes["data_transfer.a"]["metrics"]["internetOutGb"]["cost"] > 0


def _solo_cost(catalog, gb: float, basis: str) -> float:
    """What one egress node of ``gb`` costs on its own."""
    from infra_cost_model.engine import CostEngine

    model = {
        "version": "1.0",
        "workflow": {"name": "solo", "entry": "data_transfer.a",
                     "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": {"data_transfer.a": {
            "nodeType": "external", "provider": "aws",
            "service": "AWSDataTransfer", "region": "us-east-1",
            "usageMetrics": {"internetOutGb": {"unit": "GB", "value": gb}},
        }},
        "edges": [],
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        engine = CostEngine(model, catalog=catalog, time_basis=basis)
        engine.compute()
    return engine.costs["data_transfer.a"]

EXAMPLES = sorted(Path(__file__).resolve().parent.parent.glob("examples/*.yaml"))


@pytest.mark.parametrize("time_basis", ["perSecond", "monthly", "yearly"])
@pytest.mark.parametrize("example", EXAMPLES, ids=lambda p: p.stem)
def test_every_example_adds_up_on_every_time_basis(example, time_basis, capsys):
    """Each printed number keeps six significant digits or six decimals.

    So a node's printed metrics reproduce its printed total within half a
    unit in the last kept digit of each number, and a per-second cost of
    6.25e-7 prints as itself rather than as 0.000001.
    """
    assert main(["compute", str(example), "--pricing", "seed", "--format", "json",
                 "--time-basis", time_basis]) == 0
    report = json.loads(capsys.readouterr().out)
    for name, node in report["nodes"].items():
        costs = [m["cost"] for m in node["metrics"].values()]
        terms = len(costs) + 1
        assert sum(costs) == pytest.approx(node["total"], rel=terms * 5e-6,
                                           abs=terms * 5e-7), (name, node)
        for metric in node["metrics"].values():
            if metric["quantity"]:
                assert metric["unitPrice"] == pytest.approx(
                    metric["cost"] / metric["quantity"], rel=1e-5), (name, metric)
