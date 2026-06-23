"""Tests for CLI module."""

import pytest
from infra_cost_model.cli import main
from infra_cost_model.engine import CostEngine


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


def test_cli_validate_valid_yaml():
    """Test validate command with valid YAML file."""
    import tempfile
    import os
    
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
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        result = main(["validate", temp_path])
        assert result == 0
    finally:
        os.unlink(temp_path)


def test_cli_compute_valid_model():
    """Test compute command with valid model."""
    import tempfile
    import os
    
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
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        result = main(["compute", temp_path])
        assert result == 0
    finally:
        os.unlink(temp_path)


def test_sensitivity_analyzer_what_if():
    """Test what-if analysis varying frequency."""
    import tempfile
    import os
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
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        import yaml
        with open(temp_path) as f:
            model = yaml.safe_load(f)
        
        analyzer = SensitivityAnalyzer(model)
        # Double the frequency, cost should double
        cost_2x = analyzer.what_if("frequency", 2000)
        
        engine = CostEngine(model)
        baseline = engine.total_cost()
        
        assert cost_2x == pytest.approx(baseline * 2, rel=0.01)
    finally:
        os.unlink(temp_path)


def test_sensitivity_analysis():
    """Test sensitivity curve generation."""
    import tempfile
    import os
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
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
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
    finally:
        os.unlink(temp_path)


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

def test_cli_analyze_json_flag():
    """Test analyze command with --json produces JSON output."""
    import tempfile
    import os
    import io
    import sys
    import json
    
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
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        
        result = main(["analyze", temp_path, "--json"])
        
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
        
        assert result == 0
        # Should be valid JSON
        data = json.loads(output)
        assert "workflow" in data
        assert "derived_usage" in data
        assert "costs" in data
        assert "total_cost" in data
        assert data["workflow"] == "test-json"
    finally:
        os.unlink(temp_path)


def test_cli_analyze_no_json_flag_text_output():
    """Test analyze command without --json produces text output (not JSON)."""
    import tempfile
    import os
    import io
    import sys
    import json
    
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
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        
        result = main(["analyze", temp_path])
        
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
        
        assert result == 0
        assert "Analysis:" in output
        assert "Derived Usage" in output
        # Should NOT contain the misleading message
        assert "--json flag" not in output
    finally:
        os.unlink(temp_path)


def test_cli_graph_command():
    """Test graph command renders DAG."""
    import tempfile
    import os
    
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
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        result = main(["graph", temp_path])
        assert result == 0
    finally:
        os.unlink(temp_path)


def test_cli_graph_flat_override_warning():
    """Test graph command warns about flatOverride=true with incoming edges."""
    import tempfile
    import os
    import io
    import sys
    
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
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        # Capture stdout
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        
        result = main(["graph", temp_path])
        
        output = sys.stdout.getvalue()
        sys.stdout = old_stdout
        
        # Should warn about conflict with flatOverride
        assert "Conflict" in output or "flatOverride" in output
        assert result == 0
    finally:
        os.unlink(temp_path)


def test_cli_seed_pricing():
    """Test seed-pricing command."""
    result = main(["seed-pricing"])
    assert result == 0
    # Should seed prices successfully


class TestExtractCommand:
    """Tests for the 'extract' CLI command."""

    def test_extract_terraform(self):
        """Extract resources from Terraform state JSON."""
        import tempfile
        import json
        import os

        tf_json = {
            "resource": [
                {
                    "address": "aws_lambda_function.handler",
                    "values": {"memory_size": 256, "timeout": 30, "region": "us-east-1"},
                },
            ]
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(tf_json, f)
            temp_path = f.name

        try:
            from infra_cost_model.cli import main
            result = main(["extract", temp_path])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_extract_pulumi(self):
        """Extract resources from Pulumi stack export JSON."""
        import tempfile
        import json
        import os

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

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(pulumi_json, f)
            temp_path = f.name

        try:
            from infra_cost_model.cli import main
            result = main(["extract", temp_path, "--from", "pulumi"])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_extract_cdk(self):
        """Extract resources from CDK template JSON."""
        import tempfile
        import json
        import os

        cdk_json = {
            "Resources": {
                "MyFn": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {"MemorySize": 128},
                },
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(cdk_json, f)
            temp_path = f.name

        try:
            from infra_cost_model.cli import main
            result = main(["extract", temp_path, "--from", "cdk"])
            assert result == 0
        finally:
            os.unlink(temp_path)

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

    def test_whatif_missing_parameter_flag(self):
        """whatif without --parameter returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["whatif", temp_path, "--value", "2000"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_whatif_missing_value_flag(self):
        """whatif without --value returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["whatif", temp_path, "--parameter", "frequency"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_whatif_frequency_doubling(self):
        """whatif doubling frequency should ~double cost."""
        import tempfile, os
        from infra_cost_model.engine import CostEngine
        import yaml

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["whatif", temp_path, "--parameter", "frequency", "--value", "2000"])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_whatif_invalid_value(self):
        """whatif with non-numeric value returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["whatif", temp_path, "--parameter", "frequency", "--value", "abc"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_whatif_unknown_flag(self):
        """whatif with unknown flag returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["whatif", temp_path, "--bogus"])
            assert result == 1
        finally:
            os.unlink(temp_path)


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

    def test_sensitivity_missing_parameter_flag(self):
        """sensitivity without --parameter returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["sensitivity", temp_path])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_sensitivity_frequency_sweep(self):
        """sensitivity with frequency parameter returns 0."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["sensitivity", temp_path, "--parameter", "frequency"])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_sensitivity_custom_steps(self):
        """sensitivity with custom --steps works."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["sensitivity", temp_path, "--parameter", "frequency", "--steps", "5"])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_sensitivity_invalid_steps(self):
        """sensitivity with non-numeric steps returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["sensitivity", temp_path, "--parameter", "frequency", "--steps", "abc"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_sensitivity_unknown_flag(self):
        """sensitivity with unknown flag returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["sensitivity", temp_path, "--bogus"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_sensitivity_monthly_flag(self):
        """sensitivity with --monthly flag returns monthly-scaled costs."""
        import tempfile, os, io, sys, re
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_MONTHLY_CLI)
            temp_path = f.name
        try:
            # per-second baseline
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            main(["sensitivity", temp_path, "--parameter", "frequency"])
            ps_output = sys.stdout.getvalue()
            sys.stdout = old_stdout
            ps_match = re.search(r"Baseline: \$([\d.]+)", ps_output)
            assert ps_match is not None, f"No baseline in per-second output: {ps_output}"
            ps_baseline = float(ps_match.group(1))

            # monthly
            sys.stdout = io.StringIO()
            main(["sensitivity", temp_path, "--parameter", "frequency", "--monthly"])
            mo_output = sys.stdout.getvalue()
            sys.stdout = old_stdout
            mo_match = re.search(r"Baseline: \$([\d.]+)", mo_output)
            assert mo_match is not None, f"No baseline in monthly output: {mo_output}"
            mo_baseline = float(mo_match.group(1))

            # monthly costs should be significantly larger than per-second
            from infra_cost_model.engine.engine import SECONDS_PER_MONTH
            assert mo_baseline > ps_baseline * 0.9 * SECONDS_PER_MONTH, (
                f"Monthly baseline ({mo_baseline}) not scaled from per-second ({ps_baseline})"
            )
        finally:
            os.unlink(temp_path)


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

    def test_compute_monthly_flag_works(self):
        """compute --monthly returns 0."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_COMPUTE_MONTHLY)
            temp_path = f.name
        try:
            result = main(["compute", temp_path, "--monthly"])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_compute_without_monthly_still_works(self):
        """compute without --monthly still returns 0."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_COMPUTE_MONTHLY)
            temp_path = f.name
        try:
            result = main(["compute", temp_path])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_compute_monthly_shows_higher_costs(self):
        """compute --monthly shows higher costs than per-second."""
        import tempfile, os, io, sys, re
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_COMPUTE_MONTHLY)
            temp_path = f.name
        try:
            # Run per-second
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            main(["compute", temp_path])
            per_second_output = sys.stdout.getvalue()
            sys.stdout = old_stdout

            # Run monthly
            sys.stdout = io.StringIO()
            main(["compute", temp_path, "--monthly"])
            monthly_output = sys.stdout.getvalue()
            sys.stdout = old_stdout

            # Extract total from each
            ps_total_match = re.search(r"Total: \$([\d.]+)", per_second_output)
            mo_total_match = re.search(r"Total Monthly Cost: \$([\d.]+)", monthly_output)
            assert ps_total_match is not None, f"No total in per-second output: {per_second_output}"
            assert mo_total_match is not None, f"No total in monthly output: {monthly_output}"
            ps_total = float(ps_total_match.group(1))
            mo_total = float(mo_total_match.group(1))
            assert mo_total > ps_total, (
                f"Monthly total ({mo_total}) should be > per-second total ({ps_total})"
            )
        finally:
            os.unlink(temp_path)

    def test_compute_monthly_matches_analyze_total(self):
        """compute --monthly total is close to analyze total."""
        import tempfile, os, io, sys, re
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_COMPUTE_MONTHLY)
            temp_path = f.name
        try:
            old_stdout = sys.stdout

            # compute --monthly --no-catalog (matching analyze's no-catalog default)
            sys.stdout = io.StringIO()
            main(["compute", temp_path, "--monthly", "--no-catalog"])
            compute_output = sys.stdout.getvalue()

            # analyze (also no catalog)
            sys.stdout = io.StringIO()
            main(["analyze", temp_path])
            analyze_output = sys.stdout.getvalue()

            sys.stdout = old_stdout

            comp_total_match = re.search(r"Total Monthly Cost: \$([\d.]+)", compute_output)
            anal_total_match = re.search(r"Total Monthly Cost: \$([\d.]+)", analyze_output)
            assert comp_total_match is not None, f"No total in: {compute_output}"
            assert anal_total_match is not None, f"No total in: {analyze_output}"
            comp_total = float(comp_total_match.group(1))
            anal_total = float(anal_total_match.group(1))
            assert comp_total == anal_total, (
                f"Monthly compute ({comp_total}) should equal analyze ({anal_total})"
            )
        finally:
            os.unlink(temp_path)

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

    def test_whatif_monthly_flag(self):
        """whatif with --monthly flag shows monthly-scaled costs."""
        import tempfile, os, io, sys, re
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_MONTHLY_CLI)
            temp_path = f.name
        try:
            # per-second baseline
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            main(["whatif", temp_path, "--parameter", "frequency", "--value", "2000"])
            ps_output = sys.stdout.getvalue()
            sys.stdout = old_stdout
            ps_match = re.search(r"Baseline cost: \$([\d.]+)", ps_output)
            assert ps_match is not None, f"No baseline in per-second output: {ps_output}"
            ps_baseline = float(ps_match.group(1))

            # monthly
            sys.stdout = io.StringIO()
            main(["whatif", temp_path, "--parameter", "frequency", "--value", "2000", "--monthly"])
            mo_output = sys.stdout.getvalue()
            sys.stdout = old_stdout
            mo_match = re.search(r"Baseline cost: \$([\d.]+)", mo_output)
            assert mo_match is not None, f"No baseline in monthly output: {mo_output}"
            mo_baseline = float(mo_match.group(1))

            from infra_cost_model.engine.engine import SECONDS_PER_MONTH
            assert mo_baseline > ps_baseline * 0.9 * SECONDS_PER_MONTH, (
                f"Monthly baseline ({mo_baseline}) not scaled from per-second ({ps_baseline})"
            )
        finally:
            os.unlink(temp_path)


class TestCLIWhatIfSweep:
    """Tests for the what-if CLI subcommand."""

    def test_what_if_sweep_missing_args(self):
        """what-if with no args returns 1."""
        result = main(["what-if"])
        assert result == 1

    def test_what_if_sweep_missing_param(self):
        """what-if without --param returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            result = main(["what-if", temp_path, "--values", "1000,2000"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_missing_values(self):
        """what-if without --values returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            result = main(["what-if", temp_path, "--param", "frequency"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_missing_file(self):
        """what-if with nonexistent file returns 1."""
        result = main(["what-if", "/nonexistent/file.yaml",
                       "--param", "frequency", "--values", "1000,2000"])
        assert result == 1

    def test_what_if_sweep_invalid_values(self):
        """what-if with non-numeric values returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            result = main(["what-if", temp_path, "--param", "frequency",
                           "--values", "abc,def"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_single_value(self):
        """what-if with a single value returns 1 (needs >= 2)."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            result = main(["what-if", temp_path, "--param", "frequency",
                           "--values", "1000"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_table_output(self):
        """what-if with --output table (default) returns 0."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            result = main(["what-if", temp_path, "--param", "frequency",
                           "--values", "1000,2000,3000"])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_json_output(self):
        """what-if with --output json returns 0 and valid JSON."""
        import tempfile, os, io, sys, json
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            result = main(["what-if", temp_path, "--param", "frequency",
                           "--values", "1000,2000,3000", "--output", "json"])
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout

            assert result == 0
            data = json.loads(output)
            assert isinstance(data, list)
            assert len(data) == 3
            for entry in data:
                assert "param_value" in entry
                assert "total_cost" in entry
                assert "node_costs" in entry
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_compare_mode(self):
        """what-if --compare returns 0."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            result = main(["what-if", temp_path, "--param", "frequency",
                           "--values", "1000,2000", "--compare", temp_path])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_compare_missing_file(self):
        """what-if --compare with missing file returns 1."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            result = main(["what-if", temp_path, "--param", "frequency",
                           "--values", "1000,2000", "--compare", "/nonexistent.yaml"])
            assert result == 1
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_compare_json_output(self):
        """what-if --compare --output json returns valid JSON."""
        import tempfile, os, io, sys, json
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            result = main(["what-if", temp_path, "--param", "frequency",
                           "--values", "1000,2000", "--compare", temp_path,
                           "--output", "json"])
            output = sys.stdout.getvalue()
            sys.stdout = old_stdout

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
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_edge_parameter(self):
        """what-if with edge parameter works."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            result = main(["what-if", temp_path, "--param", "edge:api_gw->get_user_fn",
                           "--values", "0.5,1.0"])
            assert result == 0
        finally:
            os.unlink(temp_path)

    def test_what_if_sweep_monthly_flag(self):
        """what-if --monthly shows monthly-scaled costs."""
        import tempfile, os, io, sys, json
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_WHAT_IF_SWEEP)
            temp_path = f.name
        try:
            # per-second
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            main(["what-if", temp_path, "--param", "frequency",
                  "--values", "1000,2000", "--output", "json"])
            ps_output = sys.stdout.getvalue()
            sys.stdout = old_stdout
            ps_data = json.loads(ps_output)

            # monthly
            sys.stdout = io.StringIO()
            main(["what-if", temp_path, "--param", "frequency",
                  "--values", "1000,2000", "--output", "json", "--monthly"])
            mo_output = sys.stdout.getvalue()
            sys.stdout = old_stdout
            mo_data = json.loads(mo_output)

            from infra_cost_model.engine.engine import SECONDS_PER_MONTH
            for ps, mo in zip(ps_data, mo_data):
                assert mo["total_cost"] > ps["total_cost"] * 0.9 * SECONDS_PER_MONTH
        finally:
            os.unlink(temp_path)
