"""JSON Schema validation for cost model representation."""

import json
from importlib import resources
from jsonschema import validate, Draft202012Validator

from infra_cost_model.saas.pricing_shapes import SaaSPricingRegistry

try:  # pyyaml is an existing dependency (see validate_yaml) but stay safe offline.
    from yaml import safe_load as _yaml_safe_load, YAMLError as _YAMLError
except Exception:  # pragma: no cover - defensive fallback for missing lib
    def _yaml_safe_load(text):
        return None


SCHEMA_PATH = resources.files("infra_cost_model.schema").joinpath("cost-model.schema.json")

# A cost-model provider id is a lowercase vendor handle (aws, gcp, github...).
_PROVIDER_ID_RE = __import__("re").compile(r"^[a-z][a-z0-9_-]*$")

# Builtin cloud/SaaS providers encoded in the pricing layer.
_BASE_PROVIDERS = {"aws", "azure", "gcp", "bedrock", "openai", "external"}


def _load_known_providers() -> set[str]:
    """Known provider ids at load time: builtins plus every vendor declared under
    ``vendors/<id>/vendor.yaml``. To register a new provider, add one such file.
    """
    known = set(_BASE_PROVIDERS)
    try:
        vendors_dir = resources.files("vendors")
    except (ModuleNotFoundError, TypeError):
        return known
    for vendor_dir in sorted(vendors_dir.iterdir(), key=lambda item: item.name):
        # Underscore-prefixed directories are repository scaffolding, not providers.
        if not vendor_dir.is_dir() or vendor_dir.name.startswith("_"):
            continue
        manifest = vendor_dir.joinpath("vendor.yaml")
        try:
            data = _yaml_safe_load(manifest.read_text(encoding="utf-8")) or {}
        except (FileNotFoundError, OSError, UnicodeError, _YAMLError, ValueError):
            data = {}

        # Exactly one canonical id comes from the manifest, falling back to the directory.
        pid = data.get("id") if isinstance(data, dict) else None
        if not pid:
            pid = vendor_dir.name
        if isinstance(pid, str) and _PROVIDER_ID_RE.fullmatch(pid):
            known.add(pid)

        # Price rows may use provider aliases (for example github-copilot -> github).
        prices_file = vendor_dir.joinpath("prices.yaml")
        if prices_file.is_file():
            try:
                prices_data = _yaml_safe_load(prices_file.read_text(encoding="utf-8"))
                if isinstance(prices_data, list):
                    for row in prices_data:
                        alias = row.get("vendor") if isinstance(row, dict) else None
                        if isinstance(alias, str) and _PROVIDER_ID_RE.fullmatch(alias):
                            known.add(alias)
            except (OSError, UnicodeError, _YAMLError, ValueError):
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


def _shape_errors(model: dict) -> list[str]:
    """Report usage metrics that reference an unregistered pricing shape."""
    errors: list[str] = []
    nodes = model.get("nodes") if isinstance(model, dict) else None
    if not isinstance(nodes, dict):
        return errors

    # Importing built-ins on every validation makes this check robust when a
    # caller or test resets the mutable plugin registry.
    from infra_cost_model.saas.pricing_shapes import (
        flat_subscription,
        free_tier,
        per_unit_flat,
        transactional,
    )

    shapes = SaaSPricingRegistry.known_shapes() | {
        "flat_subscription",
        "free_tier",
        "per_unit_flat",
        "transactional",
    }
    for name, node in nodes.items():
        if not isinstance(node, dict):
            continue
        metrics = node.get("usageMetrics")
        if metrics is None:
            metric = node.get("usageMetric")
            metrics = [metric] if isinstance(metric, dict) else []
        elif isinstance(metrics, dict):
            metrics = list(metrics.values())
        if not isinstance(metrics, list):
            continue
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            shape = metric.get("shape")
            if isinstance(shape, str) and shape not in shapes:
                errors.append(
                    f"Unknown shape '{shape}' on node '{name}'. Known shapes: {sorted(shapes)}"
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
    errors.extend(_shape_errors(model))

    return errors


def validate_yaml(yaml_str: str) -> list[str]:
    """Validate a YAML cost model file against the schema."""
    import yaml

    model = yaml.safe_load(yaml_str)
    return validate_cost_model(model)