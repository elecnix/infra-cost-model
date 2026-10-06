"""Which line of the cloud bill each usage metric of a node lands on (#442).

The catalog prices a node from service codes such as ``AmazonVPC``,
while the bill calls the same money ``Amazon Virtual Private Cloud``
and ``EC2 - Other``. The mapping between the two lives in
``known_lines.yaml`` next to this module, so a model can state the
bill line of every metric, and ``validate`` can refuse a name that
matches no bill row instead of letting it read as $0.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Optional

import yaml

KNOWN_LINES_FILE = "known_lines.yaml"


@dataclass(frozen=True)
class BillingLine:
    """One line of the cloud bill, as one usage metric of one node sees it.

    ``source`` says where the line came from: ``node`` when the model
    states it in ``billingLines``, ``default`` when the known-names
    list supplied it from the node's provider, service and catalog
    metric.
    """

    node: str
    metric: str
    provider: str
    service: str
    usage_type: Optional[str]
    source: str

    def to_dict(self) -> dict[str, Any]:
        """The line as ``--json`` prints it."""
        return {
            "node": self.node,
            "metric": self.metric,
            "provider": self.provider,
            "service": self.service,
            "usageType": self.usage_type,
            "source": self.source,
        }


@lru_cache(maxsize=1)
def known_lines() -> dict[str, dict]:
    """The bundled bill-line names, keyed by vendor.

    Each vendor entry names the bill provider its lines come from, the
    usage-type prefix of each region the bill knows, and the lines
    themselves. The wheel ships the file with the package, so a
    missing file is an install problem, not a model problem.
    """
    text = (resources.files(__package__) / KNOWN_LINES_FILE).read_text(
        encoding="utf-8")
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{KNOWN_LINES_FILE}: expected a mapping of vendors")
    return data


def known_line(vendor: Optional[str], service: Optional[str],
               catalog_metric: Optional[str] = None) -> Optional[dict]:
    """The bill line a service's catalog metric bills under, if the list knows it.

    A catalog service code can bill under several lines — ``AmazonVPC``
    under both ``Amazon Virtual Private Cloud`` and ``EC2 - Other`` — so
    the catalog metric decides between them. A line that names no usage
    types prices the whole service, so it is the default for a metric no
    line names; a node states the exact line in ``billingLines``.
    """
    entry = known_lines().get(vendor) if isinstance(vendor, str) else None
    if not isinstance(entry, dict) or not isinstance(service, str):
        return None

    whole_service: Optional[dict] = None
    service_only: Optional[dict] = None
    with_metric: Optional[dict] = None
    for line in entry.get("lines") or []:
        if service not in (line.get("catalogServices") or []):
            continue
        usage_types = line.get("usageTypes") or {}
        if catalog_metric is not None and catalog_metric in usage_types:
            if with_metric is None:
                with_metric = line
        elif service_only is None:
            service_only = line
        if not usage_types and whole_service is None:
            whole_service = line
    return with_metric or whole_service or service_only


def bill_provider(vendor: Optional[str]) -> Optional[str]:
    """The bill provider a vendor's lines come from, if the list knows it."""
    entry = known_lines().get(vendor) if isinstance(vendor, str) else None
    if not isinstance(entry, dict):
        return None
    provider = entry.get("billProvider")
    return provider if isinstance(provider, str) else None


def _bill_entry(provider: str) -> Optional[dict]:
    """The vendor entry whose bill provider is ``provider``."""
    for entry in known_lines().values():
        if isinstance(entry, dict) and entry.get("billProvider") == provider:
            return entry
    return None


def resolve_billing_lines(model: dict) -> list[BillingLine]:
    """Every bill line of a model, one per node and usage metric.

    A node's ``billingLines`` entry wins over the default the
    known-names list gives the metric. Metrics the list knows nothing
    about resolve to nothing at all, so ``unmapped_billing_lines`` can
    report them: a metric with no line is a gap to fill, not a $0.
    """
    nodes = model.get("nodes") if isinstance(model, dict) else None
    if not isinstance(nodes, dict):
        return []
    resolved: list[BillingLine] = []
    for address, node in nodes.items():
        if isinstance(node, dict):
            resolved += _node_billing_lines(address, node)
    return resolved


def unmapped_billing_lines(model: dict) -> list[dict[str, str]]:
    """The usage metrics of a model that no known line covers.

    Each entry names the node, the metric and the service code the
    catalog would price it from, so the model can state the line in
    ``billingLines``. A metric a node states a line for is mapped,
    whether or not the known-names list knows that line.
    """
    nodes = model.get("nodes") if isinstance(model, dict) else None
    if not isinstance(nodes, dict):
        return []
    unmapped: list[dict[str, str]] = []
    for address, node in nodes.items():
        if not isinstance(node, dict):
            continue
        mapped = {line.metric for line in _node_billing_lines(address, node)}
        service = node.get("service")
        for metric in _usage_metrics(node):
            if metric in mapped:
                continue
            unmapped.append({
                "node": address,
                "metric": metric,
                "service": service if isinstance(service, str) else "",
            })
    return unmapped


def _usage_metrics(node: dict) -> dict:
    """A node's usage metrics, as a mapping, or an empty one.

    The schema requires a mapping; a model that reached here without one is
    reported as carrying no metrics rather than raising, because `validate`
    collects the schema error alongside this module's.
    """
    metrics = node.get("usageMetrics")
    return metrics if isinstance(metrics, dict) else {}


def billing_line_errors(model: dict) -> list[str]:
    """Why each ``billingLines`` entry of a model names a bill it cannot match.

    ``validate`` reports these, so a misspelled service name fails here
    rather than matching zero bill rows and reading as $0. A usage type
    is checked the same way when its region prefix belongs to another
    region than the node's.
    """
    nodes = model.get("nodes") if isinstance(model, dict) else None
    if not isinstance(nodes, dict):
        return []
    errors: list[str] = []
    for address, node in nodes.items():
        if isinstance(node, dict):
            errors += _node_billing_line_errors(address, node)
    return errors


def _node_billing_lines(address: str, node: dict) -> list[BillingLine]:
    """The bill lines of one node, one per usage metric."""
    vendor = node.get("provider")
    provider = bill_provider(vendor) if isinstance(vendor, str) else None
    overrides = node.get("billingLines") or {}
    catalog = _catalog_metrics(node)
    lines: list[BillingLine] = []
    for metric in _usage_metrics(node):
        override = overrides.get(metric)
        if isinstance(override, dict) and isinstance(override.get("service"), str):
            lines.append(BillingLine(
                node=address,
                metric=metric,
                provider=_override_provider(override, provider),
                service=override["service"],
                usage_type=override.get("usageType"),
                source="node",
            ))
            continue
        default = _resolved_metric(vendor, node.get("service"),
                                   catalog.get(metric) or [], node.get("region"))
        if default is not None:
            line, usage_type = default
            lines.append(BillingLine(
                node=address,
                metric=metric,
                provider=provider or "",
                service=line["service"],
                usage_type=usage_type,
                source="default",
            ))
    return lines


def _override_provider(override: dict, provider: Optional[str]) -> str:
    """The bill provider a node override names, as a string.

    A provider the model states but this module cannot read is the schema's
    error to report, so the line carries the node's own provider instead.
    """
    named = override.get("provider")
    if isinstance(named, str) and named:
        return named
    return provider or ""


def _resolved_metric(vendor: Optional[str], service: Optional[str],
                     catalog_metrics: list[str],
                     region: Optional[str]) -> Optional[tuple[dict, Optional[str]]]:
    """The default bill line for one metric, as ``(line, usage_type)``.

    ``None`` when the known-names list says nothing about the metric. A
    metric resolves to a usage type only when the line names one for the
    catalog metric; otherwise it resolves to the line's service alone.
    """
    if not isinstance(vendor, str) or not isinstance(service, str):
        return None
    fallback: Optional[tuple[dict, Optional[str]]] = None
    for name in catalog_metrics or [None]:
        line = known_line(vendor, service, name)
        if line is None:
            continue
        usage_type = (_usage_type(vendor, region, line, name)
                      if name is not None else None)
        if usage_type is not None:
            return line, usage_type
        fallback = fallback or (line, None)
    return fallback


def _usage_type(vendor: str, region: Optional[str], line: dict,
                catalog_metric: str) -> Optional[str]:
    """The usage type of a catalog metric, prefixed with the node's region.

    A suffix that already carries a region prefix keeps it, and a region
    the bill knows no prefix for leaves the suffix as it is.
    """
    suffix = (line.get("usageTypes") or {}).get(catalog_metric)
    if not isinstance(suffix, str):
        return None
    entry = known_lines().get(vendor)
    prefixes = entry.get("regionPrefixes") if isinstance(entry, dict) else None
    if not isinstance(prefixes, dict):
        return suffix
    prefix = prefixes.get(region)
    if not isinstance(prefix, str):
        return suffix
    head, _, _ = suffix.partition("-")
    if head in prefixes.values():
        return suffix
    return f"{prefix}-{suffix}"


def _node_billing_line_errors(address: str, node: dict) -> list[str]:
    """Why one node's ``billingLines`` entries cannot match the bill."""
    vendor = node.get("provider")
    metrics = node.get("usageMetrics") or {}
    errors: list[str] = []
    for metric, override in (node.get("billingLines") or {}).items():
        where = f"Node '{address}', usage metric '{metric}'"
        if metric not in metrics:
            carried = ", ".join(sorted(metrics)) or "none"
            errors.append(
                f"{where}: 'billingLines' names a metric the node does not "
                f"carry. The node carries: {carried}."
            )
            continue
        if not isinstance(override, dict):
            errors.append(
                f"{where}: 'billingLines' must map a metric to a bill line."
            )
            continue
        service = override.get("service")
        if not isinstance(service, str):
            errors.append(
                f"{where}: 'service' must be a string naming a line on the "
                f"bill, not {_shape_of(service)}."
            )
            continue
        provider = override.get("provider") or bill_provider(vendor)
        if not isinstance(provider, str):
            errors.append(
                f"{where}: 'provider' is required, because the node's "
                f"provider '{vendor}' has no known bill."
            )
            continue
        entry = _bill_entry(provider)
        if entry is None:
            known = ", ".join(sorted({
                e["billProvider"] for e in known_lines().values()
                if isinstance(e, dict) and isinstance(e.get("billProvider"), str)
            }))
            errors.append(
                f"{where}: unknown bill provider '{provider}'. "
                f"Known bill providers: {known}."
            )
            continue
        if service not in _accepted_service_names(entry):
            known = ", ".join(sorted(_service_names(entry)))
            errors.append(
                f"{where}: unknown bill service "
                f"'{service}' on bill provider "
                f"'{provider}'. Known bill services: {known}."
            )
            continue
        errors += _usage_type_errors(where, entry, node, override)
    return errors


def _shape_of(value) -> str:
    """A value's shape, for an error that must not print it as a name."""
    if value is None:
        return "absent"
    return f"a {type(value).__name__}"


def _service_names(entry: dict) -> set[str]:
    """The bill's own service names in one known-names vendor entry."""
    return {
        line["service"] for line in entry.get("lines") or []
        if isinstance(line, dict) and isinstance(line.get("service"), str)
    }


def _accepted_service_names(entry: dict) -> set[str]:
    """The names ``validate`` accepts: the bill's names plus each line's aliases."""
    accepted = _service_names(entry)
    for line in entry.get("lines") or []:
        if isinstance(line, dict):
            accepted.update(
                alias for alias in line.get("aliases") or []
                if isinstance(alias, str)
            )
    return accepted


def _usage_type_errors(where: str, entry: dict, node: dict,
                       override: dict) -> list[str]:
    """Why a usage type's region prefix belongs to another region than the node's."""
    usage_type = override.get("usageType")
    region = node.get("region")
    if not isinstance(usage_type, str) or not isinstance(region, str):
        return []
    prefixes = entry.get("regionPrefixes")
    if not isinstance(prefixes, dict):
        return []
    region_of = {prefix: named for named, prefix in prefixes.items()}
    head, _, _ = usage_type.partition("-")
    billed_in = region_of.get(head)
    if billed_in is not None and billed_in != region:
        return [
            f"{where}: usage type '{usage_type}' bills in {billed_in}, "
            f"but the node is in {region}."
        ]
    return []


def _catalog_metrics(node: dict) -> dict[str, list[str]]:
    """Map each usage metric to the catalog metrics that price it.

    The handler of the node's resource address knows this mapping, and
    the settings of the node can change it, so the bill line follows
    the same route the price takes. A node the registry does not know
    has no catalog metrics, and falls back to its service alone.
    """
    from infra_cost_model.resources.registry import ResourceRegistry

    address = node.get("resourceAddress")
    if not isinstance(address, str):
        return {}
    handler = ResourceRegistry.from_address(address, node.get("provider"))
    if handler is None:
        return {}
    mapping = handler().catalog_metrics_for(node.get("config") or {})
    catalog: dict[str, list[str]] = {}
    for metric, names in mapping.items():
        if isinstance(names, dict):
            # One logical unit bills several rows, such as an Elastic
            # Premium instance-hour that bills vCPU-hours and GiB-hours.
            catalog[metric] = list(names)
        else:
            catalog[metric] = [names]
    return catalog
