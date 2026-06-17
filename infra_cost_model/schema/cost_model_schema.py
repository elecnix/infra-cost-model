"""JSON Schema validation for cost model representation."""

import json
from pathlib import Path
from jsonschema import validate, Draft202012Validator

SCHEMA_PATH = Path(__file__).parent / "cost-model.schema.json"

def validate_cost_model(model: dict) -> list[str]:
    """Validate a cost model representation against the JSON Schema.
    
    Returns a list of validation errors, empty if valid.
    """
    schema = json.loads(SCHEMA_PATH.read_text())
    errors = []
    
    validator = Draft202012Validator(schema)
    for error in validator.iter_errors(model):
        path = ".".join(str(p) for p in error.path)
        errors.append(f"{path}: {error.message}" if path else error.message)
    
    return errors

def validate_yaml(yaml_str: str) -> list[str]:
    """Validate a YAML cost model file against the schema."""
    import yaml
    model = yaml.safe_load(yaml_str)
    return validate_cost_model(model)