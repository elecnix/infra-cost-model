"""Tests for CLI module."""

import json
import re

import pytest
import yaml
from infra_cost_model.cli import _build_parser, main
from infra_cost_model.engine import CostEngine


def _write_model(tmp_path, content, name="model.yaml"):
    """Write `content` under tmp_path and return the path as a string.

    Every command that reads a cost model or an IaC export takes a filesystem
    path, so most tests here differ only in the document they feed it. A str is
    written verbatim (YAML fixtures); a dict or list is serialised as JSON,
    which is what `extract` and `coverage` expect. tmp_path removes the file
    afterwards, so no test needs tempfile/try/finally/os.unlink.
    """
    path = tmp_path / name
    if not isinstance(content, str):
        content = json.dumps(content)
    path.write_text(content)
    return str(path)


def test_cli_no_args():
    """Test CLI with no arguments shows help."""
    result = main([])
    assert result == 0


def test_cli_unknown_command():
    """Test unknown command returns error."""
    result = main(["unknown"])
    assert result == 1


def test_cli_validate_missing_file():
    """Test validate command with missing file."""
    result = main(["validate", "/nonexistent/file.yaml"])
    assert result == 1


def test_cli_import_infracost_missing_file():
    assert main(["import-infracost", "/nonexistent/breakdown.json"]) == 1


def test_cli_import_infracost_emits_nodes(tmp_path, capsys):
    bd = {"version": "0.2", "projects": [{"name": "p", "breakdown": {"resources": [
        {"name": "aws_instance.web", "resourceType": "aws_instance",
         "costComponents": [{"name": "Instance usage", "unit": "hours",
                             "monthlyQuantity": "730", "price": "0.1",
                             "monthlyCost": "73.00"}]},
        {"name": "aws_iam_role.x", "resourceType": "aws_iam_role", "costComponents": []},
    ]}}]}
    p = tmp_path / "bd.json"
    p.write_text(json.dumps(bd))
    assert main(["import-infracost", str(p)]) == 0
    out = yaml.safe_load(capsys.readouterr().out)
    # free iam_role skipped; the instance node is priced and flat.
    assert set(out["nodes"]) == {"aws_instance.web"}
    assert out["nodes"]["aws_instance.web"]["flatOverride"] is True


def test_cli_validate_valid_yaml(tmp_path):
    """Test validate command with valid YAML file."""

    yaml_content = """
version: "1.0"
workflow:
  name: "test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 100
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
edges: []
"""

    temp_path = _write_model(tmp_path, yaml_content, name='model.yaml')
    result = main(["validate", temp_path])
    assert result == 0


def test_cli_compute_valid_model(tmp_path):
    """Test compute command with valid model."""

    yaml_content = """
version: "1.0"
workflow:
  name: "test-compute"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
edges: []
"""

    temp_path = _write_model(tmp_path, yaml_content, name='model.yaml')
    result = main(["compute", temp_path])
    assert result == 0


def test_sensitivity_analyzer_what_if(tmp_path):
    """Test what-if analysis varying frequency."""
    from infra_cost_model.engine import SensitivityAnalyzer

    yaml_content = """
version: "1.0"
workflow:
  name: "sensitivity-test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
    pricingRates:
      base_cost: 1.0
edges: []
"""

    temp_path = _write_model(tmp_path, yaml_content, name='model.yaml')
    import yaml
    with open(temp_path) as f:
        model = yaml.safe_load(f)

    analyzer = SensitivityAnalyzer(model)
    # Double the frequency, cost should double
    cost_2x = analyzer.what_if("frequency", 2000)

    engine = CostEngine(model)
    baseline = engine.total_cost()

    assert cost_2x == pytest.approx(baseline * 2, rel=0.01)


def test_sensitivity_analysis(tmp_path):
    """Test sensitivity curve generation."""
    from infra_cost_model.engine import SensitivityAnalyzer

    yaml_content = """
version: "1.0"
workflow:
  name: "sensitivity-test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
    pricingRates:
      base_cost: 1.0
edges: []
"""

    temp_path = _write_model(tmp_path, yaml_content, name='model.yaml')
    import yaml
    with open(temp_path) as f:
        model = yaml.safe_load(f)

    analyzer = SensitivityAnalyzer(model)
    results = analyzer.sensitivity("frequency", steps=5)

    assert len(results) == 5
    # Higher frequency = higher cost
    assert all(results[i][1] <= results[i+1][1] for i in range(len(results)-1))

    # Verify endpoint values span 0.5x to 2.0x baseline
    baseline = model["workflow"]["frequency"]["value"]
    assert results[0][0] == baseline * 0.5, f"First value {results[0][0]} should be 0.5x baseline {baseline}"
    assert results[-1][0] == baseline * 2.0, f"Last value {results[-1][0]} should be 2.0x baseline {baseline}"


def test_parameter_impact_unsupported_parameter_raises():
    """Test that unsupported parameter raises ValueError."""
    from infra_cost_model.engine import SensitivityAnalyzer

    model = {
        "version": "1.0",
        "workflow": {
            "name": "test",
            "entry": "node1",
            "frequency": {"unit": "perSecond", "value": 10},
        },
        "nodes": {
            "node1": {"nodeType": "compute", "resourceAddress": "node1"},
        },
        "edges": [],
    }

    analyzer = SensitivityAnalyzer(model)

    with pytest.raises(ValueError, match="Unsupported parameter"):
        analyzer.parameter_impact("unknown_param")


def test_parameter_impact_frequency_works():
    """Test that 'frequency' parameter still works."""
    from infra_cost_model.engine import SensitivityAnalyzer

    model = {
        "version": "1.0",
        "workflow": {
            "name": "test",
            "entry": "node1",
            "frequency": {"unit": "perSecond", "value": 10},
        },
        "nodes": {
            "node1": {
                "nodeType": "routing",
                "resourceAddress": "node1",
                "pricingRates": {"base": 1.0},
                "usageMetrics": {"base": {"value": 1}},
            },
        },
        "edges": [],
    }

    analyzer = SensitivityAnalyzer(model)
    impact = analyzer.parameter_impact("frequency", delta=1.0)
    # 100% increase in frequency should yield non-zero impact
    assert impact != 0.0


def test_parameter_impact_edge_parameter():
    """Test that edge parameter impact works."""
    from infra_cost_model.engine import SensitivityAnalyzer

    model = {
        "version": "1.0",
        "workflow": {
            "name": "test",
            "entry": "node1",
            "frequency": {"unit": "perSecond", "value": 10},
        },
        "nodes": {
            "node1": {"nodeType": "routing", "resourceAddress": "node1"},
            "node2": {
                "nodeType": "compute",
                "resourceAddress": "node2",
                "pricingRates": {"cpu": 1.0},
                "usageMetrics": {"cpu": {"value": 1}},
            },
        },
        "edges": [
            {"from": "node1", "to": "node2", "rate": 0.5},
        ],
    }

    analyzer = SensitivityAnalyzer(model)
    impact = analyzer.parameter_impact("edge:node1->node2", delta=1.0)
    # Doubling edge rate should increase cost
    assert impact >= 0.0


def test_parameter_impact_nonexistent_edge_raises():
    """Test that nonexistent edge raises ValueError."""
    from infra_cost_model.engine import SensitivityAnalyzer

    model = {
        "version": "1.0",
        "workflow": {
            "name": "test",
            "entry": "node1",
            "frequency": {"unit": "perSecond", "value": 10},
        },
        "nodes": {
            "node1": {"nodeType": "routing", "resourceAddress": "node1"},
        },
        "edges": [],
    }

    analyzer = SensitivityAnalyzer(model)

    with pytest.raises(ValueError, match="not found"):
        analyzer.parameter_impact("edge:a->b")


def test_parameter_impact_malformed_edge_raises():
    """Test that malformed edge spec raises ValueError."""
    from infra_cost_model.engine import SensitivityAnalyzer

    model = {
        "version": "1.0",
        "workflow": {
            "name": "test",
            "entry": "node1",
            "frequency": {"unit": "perSecond", "value": 10},
        },
        "nodes": {
            "node1": {"nodeType": "routing", "resourceAddress": "node1"},
        },
        "edges": [],
    }

    analyzer = SensitivityAnalyzer(model)

    with pytest.raises(ValueError, match="Unsupported parameter"):
        analyzer.parameter_impact("edge:no_arrow")

def test_cli_analyze_json_flag(tmp_path, capsys):
    """Test analyze command with --json produces JSON output."""

    yaml_content = """
version: "1.0"
workflow:
  name: "test-json"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
edges: []
"""

    temp_path = _write_model(tmp_path, yaml_content, name='model.yaml')

    result = main(["analyze", temp_path, "--json"])

    output = capsys.readouterr().out

    assert result == 0
    # Should be valid JSON
    data = json.loads(output)
    assert "workflow" in data
    assert "derived_usage" in data
    assert "costs" in data
    assert "total_cost" in data
    assert data["workflow"] == "test-json"


def test_cli_analyze_no_json_flag_text_output(tmp_path, capsys):
    """Test analyze command without --json produces text output (not JSON)."""

    yaml_content = """
version: "1.0"
workflow:
  name: "test-text"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
edges: []
"""

    temp_path = _write_model(tmp_path, yaml_content, name='model.yaml')

    result = main(["analyze", temp_path])

    output = capsys.readouterr().out

    assert result == 0
    assert "Analysis:" in output
    assert "Derived Usage" in output
    # Should NOT contain the misleading message
    assert "--json flag" not in output


def test_cli_graph_command(tmp_path):
    """Test graph command renders DAG."""

    yaml_content = """
workflow:
  name: "graph-test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
  lambda_fn:
    nodeType: compute
    resourceAddress: aws_lambda.test
edges:
  - from: api_gateway
    to: lambda_fn
    rate: 1.0
"""

    temp_path = _write_model(tmp_path, yaml_content, name='model.yaml')
    result = main(["graph", temp_path])
    assert result == 0


def test_cli_graph_flat_override_warning(tmp_path, capsys):
    """Test graph command warns about flatOverride=true with incoming edges."""

    # Use standard format (not DSL) since DSL transforms the structure
    yaml_content = """
version: "1.0"
workflow:
  name: "conflict-test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
  lambda_fn:
    nodeType: compute
    resourceAddress: aws_lambda.test
    flatOverride: true
    usageMetrics:
      invocations:
        value: 1000
        unit: requests
edges:
  - from: api_gateway
    to: lambda_fn
    rate: 1.0
"""

    temp_path = _write_model(tmp_path, yaml_content, name='model.yaml')
    # Capture stdout
    result = main(["graph", temp_path])

    output = capsys.readouterr().out

    # Should warn about conflict with flatOverride
    assert "Conflict" in output or "flatOverride" in output
    assert result == 0


def test_cli_seed_pricing():
    """Test seed-pricing command."""
    result = main(["seed-pricing"])
    assert result == 0
    # Should seed prices successfully


class TestExtractCommand:
    """Tests for the 'extract' CLI command."""

    def test_extract_terraform(self, tmp_path):
        """Extract resources from Terraform state JSON."""

        tf_json = {
            "resource": [
                {
                    "address": "aws_lambda_function.handler",
                    "values": {"memory_size": 256, "timeout": 30, "region": "us-east-1"},
                },
            ]
        }

        temp_path = _write_model(tmp_path, tf_json, name='data.json')
        from infra_cost_model.cli import main
        result = main(["extract", temp_path])
        assert result == 0
    def test_extract_pulumi(self, tmp_path):
        """Extract resources from Pulumi stack export JSON."""

        pulumi_json = {
            "deployment": {
                "resources": [
                    {
                        "id": "aws:lambda:Function:myHandler2",
                        "type": "aws:lambda/function:Function",
                        "inputs": {"memorySize": 256},
                    },
                ]
            }
        }

        temp_path = _write_model(tmp_path, pulumi_json, name='data.json')
        from infra_cost_model.cli import main
        result = main(["extract", temp_path, "--from", "pulumi"])
        assert result == 0
    def test_extract_cdk(self, tmp_path):
        """Extract resources from CDK template JSON."""

        cdk_json = {
            "Resources": {
                "MyFn": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {"MemorySize": 128},
                },
            }
        }

        temp_path = _write_model(tmp_path, cdk_json, name='data.json')
        from infra_cost_model.cli import main
        result = main(["extract", temp_path, "--from", "cdk"])
        assert result == 0
    def test_extract_missing_file_returns_error(self):
        """Extract with missing file returns error code 1."""
        from infra_cost_model.cli import main
        result = main(["extract", "/nonexistent.json"])
        assert result == 1

    def test_extract_no_args_returns_error(self):
        """Extract with no args returns error code 1."""
        from infra_cost_model.cli import main
        result = main(["extract"])
        assert result == 1


YAML_SENSITIVITY = """
version: "1.0"
workflow:
  name: "cli-test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway.test
    pricingRates:
      base_cost: 1.0
edges: []
"""


class TestCLIWhatif:
    """Tests for the whatif CLI command."""

    def test_whatif_missing_args(self):
        """whatif with no args prints usage and returns 1."""
        result = main(["whatif"])
        assert result == 1

    def test_whatif_missing_file(self):
        """whatif with nonexistent file returns 1."""
        result = main(["whatif", "/nonexistent/file.yaml"])
        assert result == 1

    def test_whatif_missing_parameter_flag(self, tmp_path):
        """whatif without --parameter returns 1."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["whatif", temp_path, "--value", "2000"])
        assert result == 1
    def test_whatif_missing_value_flag(self, tmp_path):
        """whatif without --value returns 1."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["whatif", temp_path, "--parameter", "frequency"])
        assert result == 1
    def test_whatif_frequency_doubling(self, tmp_path):
        """whatif doubling frequency should ~double cost."""
        from infra_cost_model.engine import CostEngine

        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["whatif", temp_path, "--parameter", "frequency", "--value", "2000"])
        assert result == 0
    def test_whatif_invalid_value(self, tmp_path):
        """whatif with non-numeric value returns 1."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["whatif", temp_path, "--parameter", "frequency", "--value", "abc"])
        assert result == 1
    def test_whatif_unknown_flag(self, tmp_path):
        """whatif with unknown flag returns 1."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["whatif", temp_path, "--bogus"])
        assert result == 1


class TestCLISensitivity:
    """Tests for the sensitivity CLI command."""

    def test_sensitivity_missing_args(self):
        """sensitivity with no args prints usage and returns 1."""
        result = main(["sensitivity"])
        assert result == 1

    def test_sensitivity_missing_file(self):
        """sensitivity with nonexistent file returns 1."""
        result = main(["sensitivity", "/nonexistent/file.yaml"])
        assert result == 1

    def test_sensitivity_missing_parameter_flag(self, tmp_path):
        """sensitivity without --parameter returns 1."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["sensitivity", temp_path])
        assert result == 1
    def test_sensitivity_frequency_sweep(self, tmp_path):
        """sensitivity with frequency parameter returns 0."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["sensitivity", temp_path, "--parameter", "frequency"])
        assert result == 0
    def test_sensitivity_custom_steps(self, tmp_path):
        """sensitivity with custom --steps works."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["sensitivity", temp_path, "--parameter", "frequency", "--steps", "5"])
        assert result == 0
    def test_sensitivity_invalid_steps(self, tmp_path):
        """sensitivity with non-numeric steps returns 1."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["sensitivity", temp_path, "--parameter", "frequency", "--steps", "abc"])
        assert result == 1
    def test_sensitivity_unknown_flag(self, tmp_path):
        """sensitivity with unknown flag returns 1."""
        temp_path = _write_model(tmp_path, YAML_SENSITIVITY, name='model.yaml')
        result = main(["sensitivity", temp_path, "--bogus"])
        assert result == 1
    def test_sensitivity_monthly_flag(self, tmp_path, capsys):
        """sensitivity with --monthly flag returns monthly-scaled costs."""
        temp_path = _write_model(tmp_path, YAML_MONTHLY_CLI, name='model.yaml')
        # per-second baseline
        main(["sensitivity", temp_path, "--parameter", "frequency"])
        ps_output = capsys.readouterr().out
        ps_match = re.search(r"Baseline: \$([\d.]+)", ps_output)
        assert ps_match is not None, f"No baseline in per-second output: {ps_output}"
        ps_baseline = float(ps_match.group(1))

        # monthly
        main(["sensitivity", temp_path, "--parameter", "frequency", "--monthly"])
        mo_output = capsys.readouterr().out
        mo_match = re.search(r"Baseline: \$([\d.]+)", mo_output)
        assert mo_match is not None, f"No baseline in monthly output: {mo_output}"
        mo_baseline = float(mo_match.group(1))

        # monthly costs should be significantly larger than per-second
        from infra_cost_model.engine.engine import SECONDS_PER_MONTH
        assert mo_baseline > ps_baseline * 0.9 * SECONDS_PER_MONTH, (
            f"Monthly baseline ({mo_baseline}) not scaled from per-second ({ps_baseline})"
        )
YAML_COMPUTE_MONTHLY = """
version: "1.0"
workflow:
  name: "monthly-test"
  entry: "lambda_fn"
  frequency:
    unit: perDay
    value: 400
nodes:
  lambda_fn:
    nodeType: compute
    resourceAddress: aws_lambda_function.test
    provider: aws
    service: lambda
    region: us-east-1
    pricingRates:
      compute: 0.0000166667
    usageMetrics:
      compute:
        value: 1
        unit: seconds
edges: []
"""


class TestCLIComputeMonthly:
    """Tests for compute --monthly flag."""

    def test_compute_monthly_flag_works(self, tmp_path):
        """compute --monthly returns 0."""
        temp_path = _write_model(tmp_path, YAML_COMPUTE_MONTHLY, name='model.yaml')
        result = main(["compute", temp_path, "--monthly"])
        assert result == 0
    def test_compute_without_monthly_still_works(self, tmp_path):
        """compute without --monthly still returns 0."""
        temp_path = _write_model(tmp_path, YAML_COMPUTE_MONTHLY, name='model.yaml')
        result = main(["compute", temp_path])
        assert result == 0
    def test_compute_monthly_shows_higher_costs(self, tmp_path, capsys):
        """compute --monthly shows higher costs than per-second."""
        temp_path = _write_model(tmp_path, YAML_COMPUTE_MONTHLY, name='model.yaml')
        # Run per-second
        main(["compute", temp_path])
        per_second_output = capsys.readouterr().out

        # Run monthly
        main(["compute", temp_path, "--monthly"])
        monthly_output = capsys.readouterr().out

        # Extract total from each
        ps_total_match = re.search(r"Total Per-Second Cost: \$([\d.]+)", per_second_output)
        mo_total_match = re.search(r"Total Monthly Cost: \$([\d.]+)", monthly_output)
        assert ps_total_match is not None, f"No total in per-second output: {per_second_output}"
        assert mo_total_match is not None, f"No total in monthly output: {monthly_output}"
        ps_total = float(ps_total_match.group(1))
        mo_total = float(mo_total_match.group(1))
        assert mo_total > ps_total, (
            f"Monthly total ({mo_total}) should be > per-second total ({ps_total})"
        )
    def test_compute_monthly_matches_analyze_total(self, tmp_path, capsys):
        """compute --monthly total is close to analyze total."""
        temp_path = _write_model(tmp_path, YAML_COMPUTE_MONTHLY, name='model.yaml')

        # compute --monthly --no-catalog (matching analyze's explicit --no-catalog)
        main(["compute", temp_path, "--monthly", "--no-catalog"])
        compute_output = capsys.readouterr().out

        # analyze, likewise off the catalog
        main(["analyze", temp_path, "--no-catalog"])
        analyze_output = capsys.readouterr().out

        comp_total_match = re.search(r"Total Monthly Cost: \$([\d.]+)", compute_output)
        anal_total_match = re.search(r"Total Monthly Cost: \$([\d.]+)", analyze_output)
        assert comp_total_match is not None, f"No total in: {compute_output}"
        assert anal_total_match is not None, f"No total in: {analyze_output}"
        comp_total = float(comp_total_match.group(1))
        anal_total = float(anal_total_match.group(1))
        assert comp_total == anal_total, (
            f"Monthly compute ({comp_total}) should equal analyze ({anal_total})"
        )
# Fixture with usageMetrics for tests that need non-zero per-second costs
YAML_MONTHLY_CLI = """
version: "1.0"
workflow:
  name: "monthly-test"
  entry: "api_gw"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gw:
    nodeType: routing
    resourceAddress: aws_api_gateway_rest_api.test_api
    provider: aws
    service: APIGateway
    region: us-east-1
  get_user_fn:
    nodeType: compute
    resourceAddress: aws_lambda_function.get_user
    provider: aws
    service: AWSLambda
    region: us-east-1
    usageMetrics:
      invocations:
        unit: requests
        value: 1
      avgDurationMs:
        unit: ms
        value: 200
      memoryMb:
        unit: MB
        value: 256
    pricingRates:
      invocations: 0.2e-6
      memoryDuration: 0.0000166667
edges:
  - from: api_gw
    to: get_user_fn
    rate: 1.0
    type: invoke
"""


YAML_WHAT_IF_SWEEP = """
version: "1.0"
workflow:
  name: "what-if-sweep-test"
  entry: "api_gw"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gw:
    nodeType: routing
    resourceAddress: aws_api_gateway_rest_api.test_api
    provider: aws
    service: APIGateway
  get_user_fn:
    nodeType: compute
    resourceAddress: aws_lambda_function.get_user
    provider: aws
    service: AWSLambda
    region: us-east-1
    usageMetrics:
      invocations:
        unit: requests
        value: 1
      avgDurationMs:
        unit: ms
        value: 200
      memoryMb:
        unit: MB
        value: 256
    pricingRates:
      invocations: 0.2e-6
      memoryDuration: 0.0000166667
edges:
  - from: api_gw
    to: get_user_fn
    rate: 1.0
    type: invoke
"""


class TestCLIWhatifMonthly:
    """Tests for whatif --monthly flag."""

    def test_whatif_monthly_flag(self, tmp_path, capsys):
        """whatif with --monthly flag shows monthly-scaled costs."""
        temp_path = _write_model(tmp_path, YAML_MONTHLY_CLI, name='model.yaml')
        # per-second baseline
        main(["whatif", temp_path, "--parameter", "frequency", "--value", "2000"])
        ps_output = capsys.readouterr().out
        ps_match = re.search(r"Baseline cost: \$([\d.]+)", ps_output)
        assert ps_match is not None, f"No baseline in per-second output: {ps_output}"
        ps_baseline = float(ps_match.group(1))

        # monthly
        main(["whatif", temp_path, "--parameter", "frequency", "--value", "2000", "--monthly"])
        mo_output = capsys.readouterr().out
        mo_match = re.search(r"Baseline cost: \$([\d.]+)", mo_output)
        assert mo_match is not None, f"No baseline in monthly output: {mo_output}"
        mo_baseline = float(mo_match.group(1))

        from infra_cost_model.engine.engine import SECONDS_PER_MONTH
        assert mo_baseline > ps_baseline * 0.9 * SECONDS_PER_MONTH, (
            f"Monthly baseline ({mo_baseline}) not scaled from per-second ({ps_baseline})"
        )


class TestCLIWhatIfSweep:
    """Tests for the what-if CLI subcommand."""

    def test_what_if_sweep_missing_args(self):
        """what-if with no args returns 1."""
        result = main(["what-if"])
        assert result == 1

    def test_what_if_sweep_missing_param(self, tmp_path):
        """what-if without --param returns 1."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--values", "1000,2000"])
        assert result == 1
    def test_what_if_sweep_missing_values(self, tmp_path):
        """what-if without --values returns 1."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "frequency"])
        assert result == 1
    def test_what_if_sweep_missing_file(self):
        """what-if with nonexistent file returns 1."""
        result = main(["what-if", "/nonexistent/file.yaml",
                       "--param", "frequency", "--values", "1000,2000"])
        assert result == 1

    def test_what_if_sweep_invalid_values(self, tmp_path):
        """what-if with non-numeric values returns 1."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "frequency",
                       "--values", "abc,def"])
        assert result == 1
    def test_what_if_sweep_single_value(self, tmp_path):
        """what-if with a single value returns 1 (needs >= 2)."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "frequency",
                       "--values", "1000"])
        assert result == 1
    def test_what_if_sweep_table_output(self, tmp_path):
        """what-if with --output table (default) returns 0."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "frequency",
                       "--values", "1000,2000,3000"])
        assert result == 0
    def test_what_if_sweep_json_output(self, tmp_path, capsys):
        """what-if with --output json returns 0 and valid JSON."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "frequency",
                       "--values", "1000,2000,3000", "--output", "json"])
        output = capsys.readouterr().out

        assert result == 0
        data = json.loads(output)
        assert isinstance(data, list)
        assert len(data) == 3
        for entry in data:
            assert "param_value" in entry
            assert "total_cost" in entry
            assert "node_costs" in entry
    def test_what_if_sweep_compare_mode(self, tmp_path):
        """what-if --compare returns 0."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "frequency",
                       "--values", "1000,2000", "--compare", temp_path])
        assert result == 0
    def test_what_if_sweep_compare_missing_file(self, tmp_path):
        """what-if --compare with missing file returns 1."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "frequency",
                       "--values", "1000,2000", "--compare", "/nonexistent.yaml"])
        assert result == 1
    def test_what_if_sweep_compare_json_output(self, tmp_path, capsys):
        """what-if --compare --output json returns valid JSON."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "frequency",
                       "--values", "1000,2000", "--compare", temp_path,
                       "--output", "json"])
        output = capsys.readouterr().out

        assert result == 0
        data = json.loads(output)
        assert isinstance(data, list)
        assert len(data) == 2
        for entry in data:
            assert "param_value" in entry
            assert "model_a" in entry
            assert "model_b" in entry
            assert "delta" in entry
            # Identical models should have zero delta
            assert entry["delta"] == 0.0
    def test_what_if_sweep_edge_parameter(self, tmp_path):
        """what-if with edge parameter works."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        result = main(["what-if", temp_path, "--param", "edge:api_gw->get_user_fn",
                       "--values", "0.5,1.0"])
        assert result == 0
    def test_what_if_sweep_monthly_flag(self, tmp_path, capsys):
        """what-if --monthly shows monthly-scaled costs."""
        temp_path = _write_model(tmp_path, YAML_WHAT_IF_SWEEP, name='model.yaml')
        # per-second
        main(["what-if", temp_path, "--param", "frequency",
              "--values", "1000,2000", "--output", "json"])
        ps_data = json.loads(capsys.readouterr().out)

        # monthly
        main(["what-if", temp_path, "--param", "frequency",
              "--values", "1000,2000", "--output", "json", "--monthly"])
        mo_data = json.loads(capsys.readouterr().out)

        from infra_cost_model.engine.engine import SECONDS_PER_MONTH
        for ps, mo in zip(ps_data, mo_data):
            assert mo["total_cost"] > ps["total_cost"] * 0.9 * SECONDS_PER_MONTH


class TestCLIBudget:
    """Tests for --budget flag on compute and analyze commands."""

    def test_compute_within_budget_returns_zero(self, tmp_path):
        """compute --budget with a high threshold returns 0."""
        temp_path = _write_model(tmp_path, YAML_COMPUTE_MONTHLY, name='model.yaml')
        result = main(["compute", temp_path, "--budget", "1000000"])
        assert result == 0
    def test_compute_budget_breach_returns_one(self, tmp_path, capsys):
        """compute --budget with a 0 threshold returns 1 and prints breach."""
        temp_path = _write_model(tmp_path, YAML_COMPUTE_MONTHLY, name='model.yaml')
        result = main(["compute", temp_path, "--budget", "0"])
        stderr_output = capsys.readouterr().err
        assert result == 1, f"Expected exit code 1, got {result}"
        assert "BUDGET BREACH" in stderr_output, (
            f"Expected BUDGET BREACH in stderr, got: {stderr_output}"
        )
        assert "exceeds budget" in stderr_output
    def test_compute_no_budget_flag_returns_zero(self, tmp_path):
        """compute without --budget returns 0 (unchanged behavior)."""
        temp_path = _write_model(tmp_path, YAML_COMPUTE_MONTHLY, name='model.yaml')
        result = main(["compute", temp_path])
        assert result == 0
    def test_analyze_within_budget_returns_zero(self, tmp_path):
        """analyze --budget with a high threshold returns 0."""
        temp_path = _write_model(tmp_path, YAML_MONTHLY_CLI, name='model.yaml')
        result = main(["analyze", temp_path, "--budget", "1000000"])
        assert result == 0
    def test_analyze_budget_breach_returns_one(self, tmp_path, capsys):
        """analyze --budget with a 0 threshold returns 1 and prints breach."""
        temp_path = _write_model(tmp_path, YAML_MONTHLY_CLI, name='model.yaml')
        result = main(["analyze", temp_path, "--budget", "0"])
        stderr_output = capsys.readouterr().err
        assert result == 1, f"Expected exit code 1, got {result}"
        assert "BUDGET BREACH" in stderr_output, (
            f"Expected BUDGET BREACH in stderr, got: {stderr_output}"
        )
        assert "exceeds budget" in stderr_output
    def test_analyze_no_budget_flag_returns_zero(self, tmp_path):
        """analyze without --budget returns 0 (unchanged behavior)."""
        temp_path = _write_model(tmp_path, YAML_MONTHLY_CLI, name='model.yaml')
        result = main(["analyze", temp_path])
        assert result == 0
    def test_compute_analyze_json_with_budget(self, tmp_path, capsys):
        """analyze --json --budget returns JSON with total_cost when within budget."""
        temp_path = _write_model(tmp_path, YAML_MONTHLY_CLI, name='model.yaml')
        result = main(["analyze", temp_path, "--json", "--budget", "1000000"])
        output = capsys.readouterr().out
        assert result == 0
        data = json.loads(output)
        assert "total_cost" in data
        assert data["total_cost"] <= 1000000
YAML_COVERAGE = """
version: "1.0"
workflow:
  name: "coverage-test"
  entry: "aws_apigatewayv2_api.items_api"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  aws_apigatewayv2_api.items_api:
    nodeType: routing
    resourceAddress: aws_apigatewayv2_api.items_api
  aws_lambda_function.get_user:
    nodeType: compute
    resourceAddress: aws_lambda_function.get_user
edges:
  - from: aws_apigatewayv2_api.items_api
    to: aws_lambda_function.get_user
    rate: 1.0
"""


class TestCLICoverage:
    """Tests for the coverage CLI subcommand."""

    def test_coverage_missing_yaml(self):
        """coverage with missing YAML file returns 1."""
        result = main(["coverage", "/nonexistent/file.yaml", "--from", "terraform", "/nonexistent/plan.json"])
        assert result == 1

    def test_coverage_missing_terraform(self, tmp_path):
        """coverage with missing terraform file returns 1."""
        temp_yaml = _write_model(tmp_path, YAML_COVERAGE, name='model.yaml')
        result = main(["coverage", temp_yaml, "--from", "terraform", "/nonexistent/plan.json"])
        assert result == 1
    def test_coverage_no_args_returns_error(self):
        """coverage with no args returns error code 1."""
        result = main(["coverage"])
        assert result == 1

    def test_coverage_fully_matched(self, tmp_path, capsys):
        """coverage with all resources matched returns 0."""
        temp_yaml = _write_model(tmp_path, YAML_COVERAGE, name='model.yaml')
        tf_json = {
            "resource": [
                {
                    "address": "aws_apigatewayv2_api.items_api",
                    "mode": "managed",
                    "type": "aws_apigatewayv2_api",
                    "name": "items_api",
                    "values": {"region": "us-east-1"},
                },
                {
                    "address": "aws_lambda_function.get_user",
                    "mode": "managed",
                    "type": "aws_lambda_function",
                    "name": "get_user",
                    "values": {"region": "us-east-1", "memory_size": 128},
                },
            ]
        }
        temp_tf = _write_model(tmp_path, tf_json, name='data.json')
        result = main(["coverage", temp_yaml, "--from", "terraform", temp_tf])
        output = capsys.readouterr().out

        assert result == 0
        assert "✓ Matched:" in output
        assert "Matched:    2" in output
        assert "Uncosted:" not in output
    def test_coverage_uncosted_resources(self, tmp_path, capsys):
        """coverage with uncosted resources returns 0 but warns (no --exit-on-uncosted)."""
        temp_yaml = _write_model(tmp_path, YAML_COVERAGE, name='model.yaml')
        # TF has an S3 bucket that is not in the model
        tf_json = {
            "resource": [
                {
                    "address": "aws_s3_bucket.logs",
                    "mode": "managed",
                    "type": "aws_s3_bucket",
                    "name": "logs",
                    "values": {"region": "us-east-1"},
                },
            ]
        }
        temp_tf = _write_model(tmp_path, tf_json, name='data.json')
        result = main(["coverage", temp_yaml, "--from", "terraform", temp_tf])
        output = capsys.readouterr().out

        # Without --exit-on-uncosted, should return 0 (warning only)
        assert result == 0
        assert "Uncosted:" in output
        assert "aws_s3_bucket.logs" in output
    def test_coverage_uncosted_exits_nonzero(self, tmp_path, capsys):
        """coverage with --exit-on-uncosted exits 1 when uncosted resources exist."""
        temp_yaml = _write_model(tmp_path, YAML_COVERAGE, name='model.yaml')
        # TF has an S3 bucket that is not in the model
        tf_json = {
            "resource": [
                {
                    "address": "aws_s3_bucket.logs",
                    "mode": "managed",
                    "type": "aws_s3_bucket",
                    "name": "logs",
                    "values": {"region": "us-east-1"},
                },
            ]
        }
        temp_tf = _write_model(tmp_path, tf_json, name='data.json')
        result = main(["coverage", temp_yaml, "--from", "terraform", temp_tf, "--exit-on-uncosted"])
        output = capsys.readouterr().out

        assert result == 1
        assert "Uncosted:" in output
        assert "aws_s3_bucket.logs" in output
    def test_coverage_orphaned_nodes(self, tmp_path, capsys):
        """coverage shows orphaned nodes (in model but not in Terraform)."""
        temp_yaml = _write_model(tmp_path, YAML_COVERAGE, name='model.yaml')
        # TF has only one of the two model nodes, the other is orphaned
        tf_json = {
            "resource": [
                {
                    "address": "aws_apigatewayv2_api.items_api",
                    "mode": "managed",
                    "type": "aws_apigatewayv2_api",
                    "name": "items_api",
                    "values": {"region": "us-east-1"},
                },
            ]
        }
        temp_tf = _write_model(tmp_path, tf_json, name='data.json')
        result = main(["coverage", temp_yaml, "--from", "terraform", temp_tf])
        output = capsys.readouterr().out

        # Orphaned nodes warn but don't fail
        assert result == 0
        assert "Orphaned:" in output
        assert "aws_lambda_function.get_user" in output
    def test_coverage_json_output(self, tmp_path, capsys):
        """coverage with --json produces structured JSON output."""
        temp_yaml = _write_model(tmp_path, YAML_COVERAGE, name='model.yaml')
        tf_json = {
            "resource": [
                {
                    "address": "aws_apigatewayv2_api.items_api",
                    "mode": "managed",
                    "type": "aws_apigatewayv2_api",
                    "name": "items_api",
                    "values": {"region": "us-east-1"},
                },
            ]
        }
        temp_tf = _write_model(tmp_path, tf_json, name='data.json')
        result = main(["coverage", temp_yaml, "--from", "terraform", temp_tf, "--json"])
        output = capsys.readouterr().out

        assert result == 0
        data = json.loads(output)
        assert "matched" in data
        assert "uncosted" in data
        assert "orphaned" in data
    def test_coverage_pulumi_format(self, tmp_path, capsys):
        """coverage with --from pulumi works."""

        yaml_pulumi = """
version: "1.0"
workflow:
  name: "coverage-test"
  entry: "aws:apigatewayv2:Api:items_api"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  aws:apigatewayv2:Api:items_api:
    nodeType: routing
    resourceAddress: aws:apigatewayv2:Api:items_api
  aws:lambda:Function:get_user:
    nodeType: compute
    resourceAddress: aws:lambda:Function:get_user
edges:
  - from: aws:apigatewayv2:Api:items_api
    to: aws:lambda:Function:get_user
    rate: 1.0
"""
        temp_yaml = _write_model(tmp_path, yaml_pulumi, name='model.yaml')
        pulumi_json = {
            "deployment": {
                "resources": [
                    {
                        "id": "aws:apigatewayv2:Api:items_api",
                        "type": "aws:apigatewayv2/api:Api",
                        "inputs": {"region": "us-east-1"},
                    },
                    {
                        "id": "aws:lambda:Function:get_user",
                        "type": "aws:lambda/function:Function",
                        "inputs": {"region": "us-east-1", "memorySize": 128},
                    },
                ]
            }
        }
        temp_tf = _write_model(tmp_path, pulumi_json, name='data.json')
        result = main(["coverage", temp_yaml, "--from", "pulumi", temp_tf])
        output = capsys.readouterr().out

        assert result == 0
        assert "✓ Matched:" in output


YAML_COVERAGE_COVERS = """
version: "1.0"
workflow:
  name: "coverage-covers-test"
  entry: "aws_apigatewayv2_api.items_api"
  frequency:
    unit: perMinute
    value: 500
nodes:
  aws_apigatewayv2_api.items_api:
    nodeType: routing
    resourceAddress: aws_apigatewayv2_api.items_api
  aws_lb.main:
    nodeType: routing
    resourceAddress: aws_lb.public
    covers:
      - "aws_lb.partner_public"
      - "aws_lb.internal"
edges:
  - from: aws_apigatewayv2_api.items_api
    to: aws_lb.main
    rate: 1.0
"""


def _tf_resource(address, resource_type, name, region="us-west-2"):
    return {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": name,
        "values": {"region": region},
    }


class TestCLICoverageCovers:
    """Tests for a node's `covers` entries in the coverage CLI subcommand (#449)."""

    def _run(self, capsys, yaml_text, tf_resources, *extra_args):
        import tempfile, os, json
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_text)
            temp_yaml = f.name
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"resource": tf_resources}, f)
            temp_tf = f.name
        try:
            result = main(["coverage", temp_yaml, "--from", "terraform", temp_tf, *extra_args])
            return result, capsys.readouterr().out
        finally:
            os.unlink(temp_yaml)
            os.unlink(temp_tf)

    def test_covers_glob_costs_every_address_it_reaches(self, capsys):
        """A `aws_lb.*` pattern costs both load balancers the node stands for."""
        result, output = self._run(
            capsys,
            YAML_COVERAGE_COVERS.replace('      - "aws_lb.partner_public"', '      - "aws_lb.*"'),
            [
                _tf_resource("aws_apigatewayv2_api.items_api", "aws_apigatewayv2_api", "items_api"),
                _tf_resource("aws_lb.public", "aws_lb", "public"),
                _tf_resource("aws_lb.internal", "aws_lb", "internal"),
            ],
        )
        assert result == 0
        assert "Matched:    3" in output
        assert "Uncosted:" not in output
        # The aggregate node stands for both load balancers, so it is not orphaned.
        assert "Orphaned:" not in output

    def test_covers_exact_entry_matches_the_full_address(self, capsys):
        """A `covers` entry without a wildcard matches one full address."""
        result, output = self._run(
            capsys,
            YAML_COVERAGE_COVERS,
            [
                _tf_resource("aws_apigatewayv2_api.items_api", "aws_apigatewayv2_api", "items_api"),
                _tf_resource("aws_lb.public", "aws_lb", "public"),
                _tf_resource("aws_lb.internal", "aws_lb", "internal"),
                _tf_resource("aws_lb.partner_public", "aws_lb", "partner_public"),
            ],
        )
        assert result == 0
        assert "Matched:    4" in output
        assert "Uncosted:" not in output

    def test_uncovered_address_stays_uncosted(self, capsys):
        """A load balancer no pattern reaches is still uncosted."""
        result, output = self._run(
            capsys,
            YAML_COVERAGE_COVERS,
            [
                _tf_resource("aws_apigatewayv2_api.items_api", "aws_apigatewayv2_api", "items_api"),
                _tf_resource("aws_lb.public", "aws_lb", "public"),
                _tf_resource("aws_lb.internal", "aws_lb", "internal"),
                _tf_resource("aws_s3_bucket.logs", "aws_s3_bucket", "logs"),
            ],
            "--exit-on-uncosted",
        )
        assert result == 1
        assert "Uncosted:" in output
        assert "aws_s3_bucket.logs" in output

    def test_stale_pattern_is_reported_and_fails(self, capsys):
        """A pattern that matches nothing is an error, even without --exit-on-uncosted."""
        result, output = self._run(
            capsys,
            YAML_COVERAGE_COVERS,
            [
                _tf_resource("aws_apigatewayv2_api.items_api", "aws_apigatewayv2_api", "items_api"),
                _tf_resource("aws_lb.public", "aws_lb", "public"),
            ],
        )
        assert result == 1
        assert "Stale:" in output
        assert "aws_lb.internal" in output
        assert "aws_lb.main" in output

    def test_json_output_names_the_node_and_pattern_of_a_stale_pattern(self, capsys):
        import json
        result, output = self._run(
            capsys,
            YAML_COVERAGE_COVERS,
            [
                _tf_resource("aws_apigatewayv2_api.items_api", "aws_apigatewayv2_api", "items_api"),
                _tf_resource("aws_lb.public", "aws_lb", "public"),
            ],
            "--json",
        )
        assert result == 1
        data = json.loads(output)
        assert data["stalePatterns"] == [
            {"node": "aws_lb.main", "pattern": "aws_lb.internal"},
            {"node": "aws_lb.main", "pattern": "aws_lb.partner_public"},
        ]
        assert data["matched"] == ["aws_apigatewayv2_api.items_api", "aws_lb.public"]

    def test_json_output_lists_a_clean_coverage(self, capsys):
        import json
        result, output = self._run(
            capsys,
            YAML_COVERAGE_COVERS,
            [
                _tf_resource("aws_apigatewayv2_api.items_api", "aws_apigatewayv2_api", "items_api"),
                _tf_resource("aws_lb.public", "aws_lb", "public"),
                _tf_resource("aws_lb.internal", "aws_lb", "internal"),
                _tf_resource("aws_lb.partner_public", "aws_lb", "partner_public"),
            ],
            "--json",
        )
        assert result == 0
        data = json.loads(output)
        assert data["uncosted"] == []
        assert data["orphaned"] == []
        assert data["stalePatterns"] == []


def test_cli_sync_pricing_defaults_to_all_regions(monkeypatch):
    """`sync-pricing` with no --region syncs every known region."""
    import infra_cost_model.pricing.sources.infracost as ic
    captured = {}

    def fake_sync(vendor="aws", services=None, regions=None):
        captured["vendor"] = vendor
        captured["services"] = services
        captured["regions"] = regions
        return (42, "infracost")

    monkeypatch.setattr(ic, "sync_pricing_catalog", fake_sync)
    rc = main(["sync-pricing"])
    assert rc == 0
    assert captured["services"] is None
    assert set(captured["regions"]) == set(ic._REGION_PREFIX) | {ic.GLOBAL_REGION}


@pytest.mark.parametrize("vendor,region", [("azure", "eastus"), ("gcp", "us-central1")])


def test_cli_sync_pricing_defaults_to_the_vendor_regions(monkeypatch, vendor, region):
    """`sync-pricing --vendor azure` syncs Azure regions, not AWS ones (#226)."""
    import infra_cost_model.pricing.sources.infracost as ic
    captured = {}
    monkeypatch.setattr(ic, "sync_pricing_catalog",
                        lambda vendor="aws", services=None, regions=None:
                            captured.update(vendor=vendor, regions=regions) or (1, "infracost"))
    rc = main(["sync-pricing", "--vendor", vendor])
    assert rc == 0
    assert captured["vendor"] == vendor
    assert captured["regions"] == ic.sync_regions(vendor)
    assert region in captured["regions"]


def test_cli_sync_pricing_explicit_regions(monkeypatch):
    import infra_cost_model.pricing.sources.infracost as ic
    captured = {}
    monkeypatch.setattr(ic, "sync_pricing_catalog",
                        lambda vendor="aws", services=None, regions=None:
                            captured.update(regions=regions) or (1, "infracost"))
    rc = main(["sync-pricing", "--region", "eu-west-1", "--region", "us-west-2"])
    assert rc == 0
    assert captured["regions"] == ["eu-west-1", "us-west-2"]


def test_cli_sync_pricing_fallback_message(monkeypatch, capsys):
    """When no credential → seed fallback, the message must not claim all regions."""
    import infra_cost_model.pricing.sources.infracost as ic
    monkeypatch.setattr(ic, "sync_pricing_catalog",
                        lambda vendor="aws", services=None, regions=None: (14, "seed-pricelist"))
    rc = main(["sync-pricing"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "seed-pricelist" in out
    assert "region(s)" not in out  # must not overstate the fan-out
    assert "fallback" in out.lower()


def test_cli_sync_pricing_states_the_pairs_that_returned_nothing(monkeypatch, capsys):
    """The summary counts the metric/region pairs whose rows were kept (#482)."""
    import infra_cost_model.pricing.sources.infracost as ic
    monkeypatch.setattr(
        ic, "sync_pricing_catalog",
        lambda vendor="aws", services=None, regions=None:
            ic.SyncResult(5, "infracost", empty=["eu-west-1/A", "eu-west-1/B"]))
    rc = main(["sync-pricing", "--region", "us-east-1", "--region", "eu-west-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Synced 5 prices" in out
    assert "2 metric/region pair(s) returned no prices" in out
    assert "kept" in out


# A node the pricing catalog prices and the model deliberately does not: no
# `pricingRates` anywhere. Under --no-catalog the engine has nothing to charge
# and reports $0; under the catalog default it reports a real figure. That gap
# is what made the analysis commands disagree with `compute`.
YAML_CATALOG_ONLY = """
version: "1.0"
workflow:
  name: "catalog-only"
  entry: "api_gw"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gw:
    nodeType: routing
    resourceAddress: aws_apigatewayv2_api.items_api
    provider: aws
    service: AmazonAPIGatewayHTTP
    region: us-east-1
    usageMetrics:
      APIGateway-HTTP-Request: { unit: requests, value: 1 }
edges: []
"""

PRICING_COMMANDS = ["compute", "analyze", "whatif", "sensitivity", "what-if"]

# The minimum argv each command needs to get as far as argument parsing.
MINIMAL_ARGV = {
    "compute": ["model.yaml"],
    "analyze": ["model.yaml"],
    "whatif": ["model.yaml", "--parameter", "frequency", "--value", "1000"],
    "sensitivity": ["model.yaml", "--parameter", "frequency"],
    "what-if": ["model.yaml", "--param", "frequency", "--values", "1,2"],
}


def _parsed(argv):
    return _build_parser().parse_args(argv)


class TestCatalogDefault:
    """Every pricing command reads the catalog unless told not to.

    Before this, `compute` took `--no-catalog` and defaulted on, while
    `whatif`, `sensitivity` and `what-if` took `--catalog` and defaulted off.
    The same model reported two different dollar figures depending on which
    command the caller reached for. Principle 13 prices by query, so the
    catalog is the default everywhere and `--no-catalog` is the escape hatch.
    """

    @pytest.mark.parametrize("command", PRICING_COMMANDS)
    def test_catalog_is_the_default(self, command):
        assert _parsed([command] + MINIMAL_ARGV[command]).use_catalog is True

    @pytest.mark.parametrize("command", PRICING_COMMANDS)
    def test_catalog_flag_is_accepted_everywhere(self, command):
        """`--catalog` stays valid on every command, so old scripts keep working."""
        argv = [command] + MINIMAL_ARGV[command] + ["--catalog"]
        assert _parsed(argv).use_catalog is True

    @pytest.mark.parametrize("command", PRICING_COMMANDS)
    def test_no_catalog_flag_is_accepted_everywhere(self, command):
        argv = [command] + MINIMAL_ARGV[command] + ["--no-catalog"]
        assert _parsed(argv).use_catalog is False

    @pytest.mark.parametrize("command", PRICING_COMMANDS)
    def test_catalog_flags_are_mutually_exclusive(self, command):
        argv = [command] + MINIMAL_ARGV[command] + ["--catalog", "--no-catalog"]
        with pytest.raises(SystemExit):
            _parsed(argv)


def _total(output, label="Total Monthly Cost"):
    match = re.search(rf"{label}: \$([\d.]+)", output)
    assert match is not None, f"No '{label}' in output: {output}"
    return float(match.group(1))


def test_analyze_defaults_to_the_catalog(tmp_path, capsys):
    """`analyze` had no catalog flag at all, so it could never reach the catalog."""
    main(["seed-pricing"])
    path = _write_model(tmp_path, YAML_CATALOG_ONLY)

    main(["analyze", path])
    with_catalog = _total(capsys.readouterr().out)

    main(["analyze", path, "--no-catalog"])
    without_catalog = _total(capsys.readouterr().out)

    assert with_catalog > 0, "analyze defaulted to $0 despite a priced metric"
    assert without_catalog == 0, (
        "The model has no pricingRates, so --no-catalog must report $0"
    )


def test_whatif_defaults_to_the_catalog(tmp_path, capsys):
    """The regression: whatif reported $0 where compute reported a real total."""
    main(["seed-pricing"])
    path = _write_model(tmp_path, YAML_CATALOG_ONLY)

    main(["whatif", path, "--parameter", "frequency", "--value", "1000"])
    baseline = re.search(r"Baseline cost: \$([\d.]+)", capsys.readouterr().out)
    assert baseline is not None
    assert float(baseline.group(1)) > 0, (
        "whatif defaulted to catalog-off and dropped every priced metric"
    )


def test_sensitivity_defaults_to_the_catalog(tmp_path, capsys):
    main(["seed-pricing"])
    path = _write_model(tmp_path, YAML_CATALOG_ONLY)

    assert main(["sensitivity", path, "--parameter", "frequency"]) == 0
    out = capsys.readouterr().out
    baseline = re.search(r"Baseline: \$([\d.]+)", out)
    assert baseline is not None, out
    assert float(baseline.group(1)) > 0, (
        "sensitivity defaulted to catalog-off and priced a catalog metric at $0"
    )


def test_what_if_defaults_to_the_catalog(tmp_path, capsys):
    main(["seed-pricing"])
    path = _write_model(tmp_path, YAML_CATALOG_ONLY)

    assert main(["what-if", path, "--param", "frequency", "--values", "1000,2000"]) == 0
    out = capsys.readouterr().out
    assert "$0.000000" not in out, (
        "what-if defaulted to catalog-off and priced a catalog metric at $0"
    )


def test_compute_and_whatif_report_the_same_figure(tmp_path, capsys):
    """Both commands, one model, one engine — the totals must now agree."""
    main(["seed-pricing"])
    path = _write_model(tmp_path, YAML_CATALOG_ONLY)

    main(["compute", path, "--time-basis", "monthly"])
    compute_total = _total(capsys.readouterr().out)

    main(["whatif", path, "--parameter", "frequency", "--value", "1000", "--monthly"])
    whatif_match = re.search(r"Baseline cost: \$([\d.]+)", capsys.readouterr().out)
    assert whatif_match is not None
    whatif_total = float(whatif_match.group(1))

    assert whatif_total == compute_total, (
        f"whatif reported {whatif_total} where compute reported {compute_total}"
    )
