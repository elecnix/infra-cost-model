"""Tests for CLI module."""

import pytest
from infra_cost_model.cli import main


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