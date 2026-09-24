"""Every bundled example computes a non-zero monthly total through the CLI.

The examples are the first thing a reader runs. Each one goes through
`infra-cost-model compute --time-basis monthly <file>` with the seed prices
and the bundled vendor prices, the catalog a user has after `seed-pricing`.
An error or a $0 total means the example no longer shows what it claims to
show.

The AWS nodes price from the catalog, not from `pricingRates` (Principle 13,
#296). A test here fails when an AWS node carries `pricingRates` or when any
metric in an example has no price.
"""

import re
import warnings
from pathlib import Path

import pytest
import yaml

from infra_cost_model import cli
from infra_cost_model.cli import main
from infra_cost_model.engine import CostEngine


EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.yaml"))
TOTAL_LINE = re.compile(r"^Total Monthly Cost: \$([0-9.]+)$", re.MULTILINE)


@pytest.fixture(autouse=True)
def cli_uses_the_seed_catalog(monkeypatch, seed_catalog):
    """`seed_catalog` loads every service in the seed file, so these tests
    don't depend on what `seed-pricing` loads (#293)."""
    monkeypatch.setattr(cli, "PricingCatalog", lambda *args, **kwargs: seed_catalog)


def load_model(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def compute_monthly_total(path: Path, capsys) -> float:
    exit_code = main(["compute", "--time-basis", "monthly", str(path)])
    captured = capsys.readouterr()
    assert exit_code == 0, f"{path.name}: {captured.err.strip()}"
    match = TOTAL_LINE.search(captured.out)
    assert match, f"{path.name}: no total in output:\n{captured.out}"
    return float(match.group(1))


def test_examples_exist():
    assert len(EXAMPLES) >= 8


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_computes_a_non_zero_monthly_total(path, capsys):
    assert compute_monthly_total(path, capsys) > 0


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_aws_nodes_have_no_pricing_rates(path):
    """An AWS node prices from the seed catalog. `pricingRates` is the escape
    hatch for a price the catalog doesn't have (Principles 9 and 13)."""
    nodes = load_model(path)["nodes"]
    with_rates = sorted(address for address, node in nodes.items()
                        if node.get("provider") == "aws" and "pricingRates" in node)
    assert with_rates == []


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_every_metric_has_a_price(path, seed_catalog):
    engine = CostEngine(load_model(path), catalog=seed_catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        engine.compute()
    assert [(m.node, m.metric) for m in engine.unpriced_metrics] == []


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_validates(path):
    assert main(["validate", str(path)]) == 0


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_every_edge_type_metric_counts_calls(path, seed_catalog, capsys):
    """No example has a metric whose edge type never reaches its node (#322)."""
    engine = CostEngine(load_model(path), catalog=seed_catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        engine.compute()
    assert engine.edge_type_warnings == []
    assert main(["validate", str(path)]) == 0
    assert "Warning:" not in capsys.readouterr().err


# Each example's monthly total, as a range. The low end applies each free
# tier once per node, which the engine does today. The high end prices every
# unit with no free tier. Once a free tier applies once per account (#294),
# a total moves up inside the range. Data transfer out already pools across
# the account: the first 100 GB a month are free (#327). A month is 2,629,800
# seconds, so 1,000 requests a minute is 43,830,000 requests. Hand checks, at
# the low end:
MONTHLY_TOTALS = {
    # ALB 730 h x $0.0225 + 487 LCU-h x $0.008 = $20.32; Lambda 42.83M paid
    # requests x $0.20/M + 5,078,750 paid GB-s x $0.0000166667 = $93.21;
    # DynamoDB 43.83M reads x $0.125/M = $5.48; NAT 730 h x $0.045 + 2,191.5
    # GB x $0.045 = $131.47; one secret $0.40.
    "always-on-infrastructure.yaml": (250.87, 257.75),
    # S3 1,500 GB x $0.023 + 30,437.5 puts x $5/M = $34.65; DynamoDB 3 GB x
    # $0.25 + 30,437.5 writes x $0.625/M = $0.77; RDS 730 h x $0.017
    # (Single-AZ) = $12.41; EventBridge 30,437.5 custom events x $1/M = $0.03
    # (no free tier); reports $0.01. Every Lambda quantity is in a free tier.
    "data-pipeline.yaml": (47.86, 50.47),
    # Stripe 175,320 orders x (2.9% of $50 + $0.30) = $306,810.00; API 438,300
    # requests x $1/M = $0.44, and the 8.77 GB of egress is inside the free
    # 100 GB; DynamoDB 788,940 reads x $0.125/M + 341,874 writes x $0.625/M =
    # $0.31; Lambda $0.04. SQS and SNS are free.
    "ecommerce-microservices.yaml": (306810.79, 306812.93),
    # The analyzer Lambda dominates: 10.96M calls x 5 GB-s, less 400,000 free,
    # is 54,387,500 GB-s x $0.0000166667 = $906.46, plus $2.56 of requests.
    # DynamoDB 35.06M writes x $0.625/M = $21.92; S3 10.96M puts x $5/M =
    # $54.79; API $21.92 and egress (219.15 GB - 100 GB free) x $0.09 = $10.72;
    # the rest $110.41.
    "event-driven-fanout.yaml": (1128.76, 1145.54),
    # Bedrock 4.383M calls x (500 x $3/M + 1,000 x $15/M) = $72,319.50;
    # DynamoDB $2.74; API, Lambda and S3 $37.62. The 21.92 GB of egress is
    # inside the free 100 GB.
    "llm-augmented-api.yaml": (72359.86, 72368.70),
    # WorkOS 2 SSO connections x $125 + 1 custom domain x $99 (900,000 MAU
    # are inside the free 1,000,000) and Datadog 6 x $23 = $487; DynamoDB 21.915M
    # reads x $0.125/M = $2.74, and no writes because no write edge reaches
    # the table (#313); API $21.92 + egress (438.3 GB - 100 GB free) x $0.09 =
    # $30.45; Lambda $15.78.
    "saas-subscription-api.yaml": (557.87, 573.75),
    # API 43.83M x $1/M = $43.83; egress (2,191.5 GB - 100 GB free) x $0.09 =
    # $188.24; DynamoDB 30.681M reads x $0.125/M + 13.149M writes x $0.625/M =
    # $12.05 (#313); Lambda $30.21.
    "serverless-api.yaml": (274.32, 290.19),
}


def test_every_aws_example_has_a_hand_checked_total():
    priced_by_vendor_rows = {"github-copilot.yaml"}
    assert set(MONTHLY_TOTALS) == {p.name for p in EXAMPLES} - priced_by_vendor_rows


@pytest.mark.parametrize("name", sorted(MONTHLY_TOTALS))
def test_example_monthly_total_matches_the_hand_check(name, capsys):
    low, high = MONTHLY_TOTALS[name]
    total = compute_monthly_total(EXAMPLES_DIR / name, capsys)
    assert low - 0.01 <= total <= high + 0.01


def test_github_copilot_prices_from_the_vendor_rows(capsys):
    """25 Business seats at $19, plus 60,000 credits against a pooled
    allowance of 1,900 per seat (47,500), with 12,500 credits over at $0.01."""
    total = compute_monthly_total(EXAMPLES_DIR / "github-copilot.yaml", capsys)
    assert total == pytest.approx(25 * 19.0 + (60_000 - 25 * 1_900) * 0.01)


def test_multi_workflow_example_reports_every_workflow(capsys):
    """`data-pipeline.yaml` uses the `workflows` array the schema allows."""
    assert main(["compute", "--time-basis", "monthly",
                 str(EXAMPLES_DIR / "data-pipeline.yaml")]) == 0
    out = capsys.readouterr().out
    assert "data-pipeline" in out
    assert "daily-analytics" in out


MULTI_WORKFLOW = EXAMPLES_DIR / "data-pipeline.yaml"


def test_analyze_names_every_workflow(capsys):
    assert main(["analyze", str(MULTI_WORKFLOW)]) == 0
    assert "Analysis: data-pipeline, daily-analytics" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["whatif", "--parameter", "frequency", "--value", "2"],
    ["sensitivity", "--parameter", "frequency"],
    ["what-if", "--param", "frequency", "--values", "1,2"],
], ids=lambda argv: argv[0])
def test_single_workflow_commands_refuse_a_workflows_array(argv, capsys):
    """These sweeps vary one workflow's frequency, so they name the limit
    instead of failing with a KeyError traceback."""
    assert main([argv[0], str(MULTI_WORKFLOW), *argv[1:]]) == 1
    assert "single 'workflow'" in capsys.readouterr().err


def test_what_if_compare_refuses_a_workflows_array(capsys):
    single = EXAMPLES_DIR / "serverless-api.yaml"
    assert main(["what-if", str(single), "--param", "frequency", "--values", "1,2",
                 "--compare", str(MULTI_WORKFLOW)]) == 1
    assert "single 'workflow'" in capsys.readouterr().err
