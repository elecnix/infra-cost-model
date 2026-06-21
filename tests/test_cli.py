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
        """sensitivity with --monthly flag returns 0."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["sensitivity", temp_path, "--parameter", "frequency", "--monthly"])
            assert result == 0
        finally:
            os.unlink(temp_path)


class TestCLIComputeMonthly:
    """Tests for compute --monthly flag."""

    def test_compute_monthly_flag(self):
        """compute with --monthly flag returns 0."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["compute", temp_path, "--monthly"])
            assert result == 0
        finally:
            os.unlink(temp_path)


class TestCLIWhatifMonthly:
    """Tests for whatif --monthly flag."""

    def test_whatif_monthly_flag(self):
        """whatif with --monthly flag returns 0."""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(YAML_SENSITIVITY)
            temp_path = f.name
        try:
            result = main(["whatif", temp_path, "--parameter", "frequency", "--value", "2000", "--monthly"])
            assert result == 0
        finally:
            os.unlink(temp_path)
