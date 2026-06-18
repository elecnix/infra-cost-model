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