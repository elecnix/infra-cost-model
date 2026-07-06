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

import re

# Terraform resource-type prefix → cost-model provider.
_PROVIDER_PREFIX = {"aws": "aws", "google": "gcp", "azurerm": "azure", "azuread": "azure"}


def _provider_for(resource_type: str) -> str:
    """Infer the cost-model provider from a Terraform resource type."""
    prefix = (resource_type or "").split("_", 1)[0]
    return _PROVIDER_PREFIX.get(prefix, "unknown")


def _to_float(value) -> float | None:
    """Infracost money/quantity fields are strings; tolerate None/blank."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slug(text: str) -> str:
    """A YAML/metric-key-safe slug for a cost-component name."""
    return re.sub(r"[^0-9a-zA-Z_]+", "-", (text or "").strip()).strip("-").lower() or "component"


def _iter_components(resource: dict, prefix: str = ""):
    """Yield (metric_key, monthly_cost) for a resource's components and, depth-first,
    its subresources'. Subresource keys are namespaced by the subresource name so
    same-named components (e.g. two "Storage" lines) never collide."""
    for comp in resource.get("costComponents") or []:
        cost = _to_float(comp.get("monthlyCost"))
        if cost is None:
            continue
        yield f"{prefix}{_slug(comp.get('name'))}", cost
    for sub in resource.get("subresources") or []:
        sub_prefix = f"{prefix}{_slug(sub.get('name'))}."
        yield from _iter_components(sub, sub_prefix)


def import_breakdown(breakdown_json: dict) -> dict[str, dict]:
    """Convert Infracost breakdown JSON into cost-model nodes.

    Returns ``{resource_address: node}``. One node per *costed* resource (those
    with at least one priced cost component, directly or in a subresource);
    free resources are skipped. Each node is a ``flatOverride`` leaf whose
    ``fixed`` metrics carry the components' monthly costs (rate 1.0), so it
    prices to the resource's Infracost monthly total with no catalog lookup.
    """
    nodes: dict[str, dict] = {}
    for project in breakdown_json.get("projects") or []:
        resources = (project.get("breakdown") or {}).get("resources") or []
        for resource in resources:
            address = resource.get("name")
            if not address:
                continue
            metrics = {}
            rates = {}
            for key, cost in _iter_components(resource):
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
