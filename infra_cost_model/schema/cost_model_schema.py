"""JSON Schema validation for cost model representation."""

import json
from pathlib import Path
from jsonschema import validate, Draft202012Validator

try:  # pyyaml is an existing dependency (see validate_yaml) but stay safe offline.
    from yaml import safe_load as _yaml_safe_load, YAMLError as _YAMLError
except Exception:  # pragma: no cover - defensive fallback for missing lib
    def _yaml_safe_load(text):
        return None


SCHEMA_PATH = Path(__file__).parent / "cost-model.schema.json"

# A cost-model provider id is a lowercase vendor handle (aws, gcp, github...).
_PROVIDER_ID_RE = __import__("re").compile(r"^[a-z][a-z0-9_-]*$")

# Builtin cloud/SaaS providers encoded in the pricing layer.
_BASE_PROVIDERS = {"aws", "azure", "gcp", "bedrock", "openai", "external"}


def _load_known_providers() -> set[str]:
    """Known provider ids at load time: builtins plus every vendor declared under
    ``vendors/<id>/vendor.yaml``. To register a new provider, add one such file.
    """
    known = set(_BASE_PROVIDERS)
    vendors_dir = Path(__file__).resolve().parents[2] / "vendors"
    if not vendors_dir.is_dir():
        return known
    for vf in sorted(vendors_dir.glob("*/vendor.yaml")):
        try:
            data = _yaml_safe_load(vf.read_text()) or {}
        except (_YAMLError, ValueError):
            continue  # malformed vendor manifest does not block schema validation
        
        # Register from vendor.yaml (prefer id, then provider, then dir name)
        if isinstance(data, dict):
            pid = data.get("id") or data.get("provider") or vf.parent.name
        else:
            pid = vf.parent.name
        if isinstance(pid, str) and _PROVIDER_ID_RE.match(pid):
            known.add(pid)

        # Also register from prices.yaml in the same directory
        prices_file = vf.parent / "prices.yaml"
        if prices_file.exists():
            try:
                prices_data = _yaml_safe_load(prices_file.read_text())
                if isinstance(prices_data, list):
                    for row in prices_data:
                        if isinstance(row, dict):
                            v = row.get("vendor")
                            if isinstance(v, str) and _PROVIDER_ID_RE.match(v):
                                known.add(v)
            except (_YAMLError, ValueError):
                pass
    return known


# Resolved once at load time so every validation pass consults a stable set.
KNOWN_PROVIDERS = sorted(_load_known_providers())
_KNOWN_PROVIDERS_SET = set(KNOWN_PROVIDERS)


def _provider_errors(model: dict) -> list[str]:
    """Clear, actionable error for any node whose ``provider`` is not recognized."""
    errors: list[str] = []
    if not isinstance(model, dict):
        return errors
    nodes = model.get("nodes")
    if not isinstance(nodes, dict):
        return errors
    known_list = ", ".join(KNOWN_PROVIDERS)
    for name, node in nodes.items():
        if not isinstance(node, dict):
            continue
        provider = node.get("provider")
        # Grossly malformed ids (uppercase / '!…') are already reported by the JSON-Schema `pattern`; only
        # flag well-formed lowercase ids that simply aren't part of the known set.
        if not (isinstance(provider, str) and _PROVIDER_ID_RE.match(provider)):
            continue
        if provider in _KNOWN_PROVIDERS_SET:
            continue
        prefix = f"nodes.{name}.provider: " if isinstance(name, str) else ""
        errors.append(
            f'{prefix}Unknown provider "{provider}". '
            f'Known providers: {known_list} (add a vendors/<id>/vendor.yaml to register it).'
        )
    return errors


def validate_cost_model(model: dict) -> list[str]:
    """Validate a cost model representation against the JSON Schema.

    Returns a list of validation errors, empty if valid. The schema uses an open
    ``provider`` pattern (``^[a-z][a-z0-9_-]*$``) so vendor ids like ``github`` are
    syntactically accepted; this layer additionally enforces that any declared
    provider id is one of the known providers loaded at load time, emitting a clear
    message instead of an opaque downstream type error.
    """
    schema = json.loads(SCHEMA_PATH.read_text())
    errors: list[str] = []

    validator = Draft202012Validator(schema)
    for error in validator.iter_errors(model):
        path = ".".join(str(p) for p in error.path)
        errors.append(f"{path}: {error.message}" if path else error.message)

    # Semantic provider whitelist (see DP: validate against loaded vendor ids at load time).
    errors.extend(_provider_errors(model))

    return errors


def validate_yaml(yaml_str: str) -> list[str]:
    """Validate a YAML cost model file against the schema."""
    import yaml

    model = yaml.safe_load(yaml_str)
    return validate_cost_model(model)