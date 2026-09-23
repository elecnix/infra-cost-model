"""Every bundled example computes a non-zero monthly total through the CLI.

The examples are the first thing a reader runs. Each one goes through
`infra-cost-model compute --time-basis monthly <file>` with the default
catalog (seed prices plus the bundled vendor prices), the same path a user
takes from a source checkout. An error or a $0 total means the example no
longer shows what it claims to show.
"""

import re
from pathlib import Path

import pytest

from infra_cost_model.cli import main


EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLES = sorted(EXAMPLES_DIR.glob("*.yaml"))
TOTAL_LINE = re.compile(r"^Total Monthly Cost: \$([0-9.]+)$", re.MULTILINE)


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
def test_example_validates(path):
    assert main(["validate", str(path)]) == 0


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
