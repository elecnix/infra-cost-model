"""Import Infracost breakdown JSON as pre-priced cost-model nodes.

Blanket pricing for the long tail: any resource Infracost supports becomes a
priced node without a hand-written ``ResourceType`` handler or a
``_METRIC_DESCRIPTORS`` entry. Infracost already encodes the extract + cost
components + product filters for hundreds of resources; this ingests its
``infracost breakdown --path <dir> --format json`` output (schema 0.x) and turns
each resource into a ``flatOverride`` node whose ``fixed`` metrics mirror
Infracost's own per-component monthly costs.

This is the generic escape hatch (DP#9): it covers the static, always-on tail —
the resources otherwise forced onto hand-written ``flatOverride`` nodes. Native
handlers stay for the request-path resources where the DAG derives usage from
upstream flow; for a resource that has both, prefer the handler.

See https://www.infracost.io/docs/features/cli_commands/#json-output
"""

from __future__ import annotations

import math
import re
import warnings

# Terraform resource-type prefix → cost-model provider.
_PROVIDER_PREFIX = {"aws": "aws", "google": "gcp", "azurerm": "azure", "azuread": "azure"}


def _provider_for(resource_type: str) -> str:
    """Infer the cost-model provider from a Terraform resource type."""
    prefix = (resource_type or "").split("_", 1)[0]
    return _PROVIDER_PREFIX.get(prefix, "external")


def _to_float(value) -> float | None:
    """Infracost money/quantity fields are strings; tolerate None/blank."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _monthly_cost(value, where: str) -> float | None:
    """A component's monthly cost, or None, with a warning for one it drops.

    Infracost leaves the cost out (None) for a component it can't price. Any
    other value that isn't a finite, non-negative number would price a node
    below zero or as NaN, so it is dropped and named.
    """
    if value is None or value == "":
        return None
    cost = _to_float(value)
    if cost is None or not math.isfinite(cost) or cost < 0:
        warnings.warn(f"{where}: monthlyCost {value!r} is not a price, so the "
                      f"import leaves the component out.")
        return None
    return cost


def _slug(text: str) -> str:
    """A YAML/metric-key-safe slug for a cost-component name."""
    return re.sub(r"[^0-9a-zA-Z_]+", "-", (text or "").strip()).strip("-").lower() or "component"


def _iter_components(resource: dict, prefix: str = "", address: str = ""):
    """Yield (metric_key, monthly_cost) for a resource's components and, depth-first,
    its subresources'. Subresource keys are namespaced by the subresource name so
    same-named components (e.g. two "Storage" lines) never collide."""
    for comp in _objects(resource.get("costComponents"), f"{address} costComponents"):
        cost = _monthly_cost(comp.get("monthlyCost"),
                             f"{address} {prefix}{comp.get('name')}".strip())
        if cost is None:
            continue
        yield f"{prefix}{_slug(comp.get('name'))}", cost
    for sub in _objects(resource.get("subresources"), f"{address} subresources"):
        sub_prefix = f"{prefix}{_slug(sub.get('name'))}."
        yield from _iter_components(sub, sub_prefix, address)


def _objects(value, where: str) -> list:
    """A list of JSON objects from the breakdown, or a ValueError naming where."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"{where} is not a list of objects, so this is not an "
                         f"`infracost breakdown --format json` file")
    return value


def import_breakdown(breakdown_json: dict) -> dict[str, dict]:
    """Convert Infracost breakdown JSON into cost-model nodes.

    Returns ``{resource_address: node}``. One node per *costed* resource (those
    with at least one priced cost component, directly or in a subresource);
    free resources are skipped. Each node is a ``flatOverride`` leaf whose
    ``fixed`` metrics carry the components' monthly costs (rate 1.0), so it
    prices to the resource's Infracost monthly total with no catalog lookup.
    """
    if not isinstance(breakdown_json, dict):
        raise ValueError("the file is not a JSON object, so this is not an "
                         "`infracost breakdown --format json` file")
    nodes: dict[str, dict] = {}
    for project in _objects(breakdown_json.get("projects"), "projects"):
        breakdown = project.get("breakdown") or {}
        if not isinstance(breakdown, dict):
            raise ValueError("a project's breakdown is not an object, so this is not "
                             "an `infracost breakdown --format json` file")
        for resource in _objects(breakdown.get("resources"), "breakdown.resources"):
            address = resource.get("name")
            if not address:
                continue
            metrics = {}
            rates = {}
            for key, cost in _iter_components(resource, address=address):
                # Disambiguate the rare within-resource key collision.
                if key in metrics:
                    key = f"{key}-{len(metrics)}"
                metrics[key] = {"unit": "USD/mo", "value": cost, "fixed": True}
                rates[key] = 1.0
            if not metrics:
                continue  # free resource — nothing to cost
            resource_type = resource.get("resourceType", "")
            nodes[address] = {
                "nodeType": "storage",
                "resourceAddress": address,
                "provider": _provider_for(resource_type),
                "service": resource_type,
                "region": resource.get("region"),
                "flatOverride": True,
                "usageMetrics": metrics,
                "pricingRates": rates,
            }
    return nodes
