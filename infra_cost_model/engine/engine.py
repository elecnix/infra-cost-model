"""
Cost engine: DAG traversal, workload derivation, and cost aggregation.

This module implements Principles 1, 2, 3, 5:
- Workload derivation: Compute derived usage by propagating frequency through DAG
- DAG validation: Cycle detection and edge validation
- Cost aggregation: Aggregate costs bottom-up from derived usage
- Resource x Cost Model join: Combine representations to produce costs
"""

import warnings
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Optional

from infra_cost_model.pricing.catalog import SECONDS_PER_MONTH, PricingCatalog
from infra_cost_model.pricing.free_tiers import (
    ACCOUNT, FREE_ALLOWANCE_REGIONS, SharedFreeAllowance, free_tier_scope,
    shared_free_allowance,
)
from infra_cost_model.pricing.global_services import (
    GLOBAL_PRICE_REGIONS, is_global_metric,
)
from infra_cost_model.pricing.price_pools import price_pool
from infra_cost_model.version_requirement import require_engine


# Edge types an edge's ``type`` and a usage metric's ``edgeType`` accept. An
# edge without a type is an "invoke" edge.
EDGE_TYPES = ("read", "write", "invoke")
DEFAULT_EDGE_TYPE = "invoke"


@dataclass
class DerivedUsage:
    """Derived usage metrics for a single node."""
    resource_address: str
    invocation_count: float  # How many times this node executes
    usage_metrics: dict[str, float] = field(default_factory=dict)
    data_in: float = 0.0  # Total data received (bytes from incoming edges)
    input_tokens: float = 0.0  # Total input tokens received (from upstream edges)
    output_tokens: float = 0.0  # Total output tokens produced (for LLM nodes)
    edge_types: set[str] = field(default_factory=set)  # Edge types feeding this node
    # Invocations per second that arrive over each edge type. Entry traffic
    # counts as "invoke", the type of an edge that declares none (#313).
    invocations_by_edge_type: dict[str, float] = field(default_factory=dict)

    def invocations_for(self, metric_def) -> float:
        """How many of this node's invocations a usage metric counts.

        A metric that declares ``edgeType`` counts only the calls that arrive
        over edges of that type, so a DynamoDB table's read metric does not
        also count its writes (#313). A metric without ``edgeType`` counts
        every invocation.
        """
        edge_type = metric_def.get("edgeType") if isinstance(metric_def, dict) else None
        if edge_type is None:
            return self.invocation_count
        if edge_type not in EDGE_TYPES:
            raise ValueError(
                f"Unknown edgeType '{edge_type}' on a usage metric of "
                f"'{self.resource_address}'. Valid edge types: "
                f"{', '.join(EDGE_TYPES)}"
            )
        return self.invocations_by_edge_type.get(edge_type, 0.0)


@dataclass(frozen=True)
class UnpricedMetric:
    """A usage metric the engine found no price for.

    The node's cost leaves this metric out. ``quantity`` is the amount the
    engine tried to price, in ``time_basis`` units (per second, per month or
    per year).
    """
    node: str
    metric: str
    provider: Optional[str]
    service: str
    region: Optional[str]
    quantity: float
    time_basis: str

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        period = {"perSecond": "second", "monthly": "month", "yearly": "year"}.get(
            self.time_basis, self.time_basis
        )
        return (
            f"Node '{self.node}': no price for metric '{self.metric}' "
            f"(provider {self.provider}, service {self.service or '-'}, "
            f"region {self.region}). The total leaves out "
            f"{self.quantity:g} units per {period}. Add a catalog row, a "
            f"pricingRates entry, or a shape for this metric."
        )


class UnpricedMetricWarning(UserWarning):
    """Emitted once per metric the engine could not price.

    ``unpriced`` carries the ``UnpricedMetric`` record.
    """

    def __init__(self, unpriced: UnpricedMetric):
        super().__init__(
            f"Node '{unpriced.node}': no price for metric '{unpriced.metric}' "
            f"(provider {unpriced.provider}, service {unpriced.service or '-'}, "
            f"region {unpriced.region}). The total leaves it out."
        )
        self.unpriced = unpriced


@dataclass
class _Miss:
    """A metric the aggregator could not price, before time-basis scaling."""
    node: str
    metric: str
    provider: Optional[str]
    service: str
    region: Optional[str]
    quantity: float  # per second when variable, per month when fixed
    fixed: bool


def _metric_is_fixed(metric_def, flat_override: bool) -> bool:
    """Whether a single usage metric is a fixed (frequency-independent) total.

    A metric is fixed when the containing node sets flatOverride=true (legacy,
    all metrics fixed) or when the metric itself carries `fixed: true`
    (per-metric flag, Issue #196). Fixed metrics use their value directly as a
    flat monthly total instead of scaling by the derived invocation count.
    """
    if flat_override:
        return True
    return isinstance(metric_def, dict) and bool(metric_def.get("fixed", False))


def _node_has_fixed_cost(node: dict) -> bool:
    """Whether a node carries any always-on (fixed) cost component.

    Always-on nodes (a load balancer, NAT gateway, a reserved instance) are
    costed without a synthetic incoming edge and are not reported as
    unreachable (Issue #196).
    """
    if node.get("flatOverride", False):
        return True
    metrics = node.get("usageMetrics", {}) or {}
    return any(
        isinstance(m, dict) and m.get("fixed", False) for m in metrics.values()
    )


def _node_is_fully_fixed(node: dict) -> bool:
    """Whether every cost component of a node is fixed (no usage-driven metric).

    Used for the DP#9 conflict warning: only a fully-fixed node that also
    receives DAG edges is a genuine flat-vs-derived conflict. A node mixing
    fixed and usage-driven metrics legitimately consumes its incoming edges.
    """
    if node.get("flatOverride", False):
        return True
    metrics = node.get("usageMetrics", {}) or {}
    if not metrics:
        return False
    return all(
        isinstance(m, dict) and m.get("fixed", False) for m in metrics.values()
    )


def catalog_location_error(address: str, node: dict) -> Optional[str]:
    """Why a catalog lookup for this node cannot run, or None if it can.

    Pricing is a layer separate from the graph (Principle 6), so the engine
    never guesses a node's provider or region (#164). A catalog lookup needs
    both. Only flat and tiered nodes query the catalog, and only for metrics
    without a SaaS ``shape``: a shape handler prices its metric from inline
    parameters. A node whose metrics all have a shape needs neither field
    (#273). ``compute`` with a catalog raises this message and ``validate``
    reports it, so the two always agree.
    """
    if node.get("pricingModel", "flat") not in ("flat", "tiered"):
        return None
    metrics = node.get("usageMetrics") or {}
    if all(isinstance(m, dict) and m.get("shape") is not None
           for m in metrics.values()):
        return None
    if node.get("provider") is None:
        return (
            f"Node '{address}' is missing required 'provider' field. "
            f"Per Principle 6, the cost engine is provider-agnostic: "
            f"provider must be specified explicitly on each node "
            f"(e.g., 'aws', 'gcp', 'azure')."
        )
    if node.get("region") is None:
        return (
            f"Node '{address}' is missing required 'region' field. "
            f"Region must be specified explicitly on each node "
            f"(e.g., 'us-east-1', 'eu-west-1', 'us-central1')."
        )
    return None


def catalog_location_errors(model: dict) -> list[str]:
    """``catalog_location_error`` for every node of a model."""
    nodes = model.get("nodes") if isinstance(model, dict) else None
    if not isinstance(nodes, dict):
        return []
    errors = []
    for address, node in nodes.items():
        if isinstance(node, dict):
            error = catalog_location_error(address, node)
            if error is not None:
                errors.append(error)
    return errors


class EdgeTypeMetricWarning(UserWarning):
    """A usage metric counts no calls because no edge of its type reaches its node.

    Emitted once per (node, metric) pair that ``edge_type_metric_warning``
    reports (#322).
    """


def edge_type_metric_warning(address: str, node: dict, metric_name: str,
                             metric_def, received: set[str]) -> Optional[str]:
    """Why a usage metric's ``edgeType`` counts no calls, or None if it counts some.

    A metric with ``edgeType`` counts only the calls that arrive over edges of
    that type (#313). If the node receives calls over other edge types and none
    over this one, the metric counts no calls, which usually means an edge
    lacks its ``type``. A node that receives no calls at all is left to the unreachable
    node warning, and a fixed metric ignores ``edgeType``. ``compute`` warns
    with this message and ``validate`` reports it, so the two always agree.
    """
    if not isinstance(metric_def, dict):
        return None
    edge_type = metric_def.get("edgeType")
    if edge_type not in EDGE_TYPES or not received or edge_type in received:
        return None
    if _metric_is_fixed(metric_def, node.get("flatOverride", False)):
        return None
    return (
        f"Node '{address}': usage metric '{metric_name}' counts only calls over "
        f"{edge_type} edges, and no {edge_type} edge reaches the node, so the "
        f"metric counts no calls. The node receives calls over "
        f"{', '.join(sorted(received))} edges. Set 'type: {edge_type}' on the "
        f"edge that carries these calls, or remove the metric."
    )


def edge_type_metric_warnings(model: dict) -> list[str]:
    """``edge_type_metric_warning`` for every metric of a model, from its edges.

    ``validate`` does not derive traffic, so it takes the edge types a node
    receives from the edges that end at the node, plus ``invoke`` for a
    workflow's entry node.
    """
    if not isinstance(model, dict) or not isinstance(model.get("nodes"), dict):
        return []
    received: dict[str, set[str]] = defaultdict(set)
    workflows = model.get("workflows") or [model.get("workflow")]
    for workflow in workflows:
        if isinstance(workflow, dict) and workflow.get("entry"):
            received[workflow["entry"]].add(DEFAULT_EDGE_TYPE)
    for edge in model.get("edges") or []:
        if isinstance(edge, dict) and edge.get("to"):
            received[edge["to"]].add(edge.get("type", DEFAULT_EDGE_TYPE))
    return _edge_type_metric_warnings(model["nodes"], received)


def _edge_type_metric_warnings(nodes: dict,
                               received: dict[str, set[str]]) -> list[str]:
    """Apply ``edge_type_metric_warning`` to each node's metrics."""
    messages = []
    for address, node in nodes.items():
        if not isinstance(node, dict):
            continue
        for metric_name, metric_def in (node.get("usageMetrics") or {}).items():
            message = edge_type_metric_warning(
                address, node, metric_name, metric_def, received.get(address, set()))
            if message is not None:
                messages.append(message)
    return messages


class DAGValidator:
    """Validates DAG structure for cost model."""

    def __init__(self, nodes: dict[str, dict], edges: list[dict]):
        self.nodes = nodes
        self.edges = edges
        self.errors: list[str] = []

    def validate(self) -> bool:
        """Run all validations. Returns True if valid."""
        self.errors = []
        self._check_all_edges_exist()
        self._check_cycles()
        return len(self.errors) == 0

    def _check_all_edges_exist(self) -> None:
        """Verify all edge sources and targets reference existing nodes."""
        node_addresses = set(self.nodes.keys())

        for i, edge in enumerate(self.edges):
            if edge.get("from") not in node_addresses:
                self.errors.append(
                    f"Edge {i}: 'from' node '{edge.get('from')}' not found in nodes"
                )
            if edge.get("to") not in node_addresses:
                self.errors.append(
                    f"Edge {i}: 'to' node '{edge.get('to')}' not found in nodes"
                )

    def _check_cycles(self) -> None:
        """Detect cycles using DFS. DAG must have no cycles."""
        graph: dict[str, list[str]] = defaultdict(list)
        for edge in self.edges:
            graph[edge.get("from")].append(edge.get("to"))

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {addr: WHITE for addr in self.nodes}

        def dfs(node: str, path: list[str]) -> bool:
            if node not in color:
                return False
            if color[node] == GRAY:
                self.errors.append(f"Cycle detected: {' → '.join(path + [node])}")
                return True
            if color[node] == BLACK:
                return False

            color[node] = GRAY
            for neighbor in graph.get(node, []):
                if dfs(neighbor, path + [node]):
                    return True
            color[node] = BLACK
            return False

        for node in self.nodes:
            if color[node] == WHITE:
                dfs(node, [])


class WorkloadDeriver:
    """Derives node usage by propagating frequency through DAG."""

    def __init__(self, workflow: dict, nodes: dict[str, dict], edges: list[dict],
                 parameters: dict[str, float] = None):
        self.workflow = workflow
        self.nodes = nodes
        self.edges = edges
        self.parameters = parameters or {}
        self.derived_usage: dict[str, DerivedUsage] = {}

    def derive(self) -> dict[str, DerivedUsage]:
        """Traverse DAG top-down in topological order to compute derived usage.

        Uses Kahn's algorithm: a node propagates downstream only after all of
        its incoming edges have been processed, ensuring correct accumulation
        for multi-path DAGs (e.g., A→B, A→C, C→B, B→D where C contributes
        to B before B propagates to D).

        Returns:
            Dict mapping resource address to DerivedUsage.

        Raises:
            ValueError: If the entry node address does not exist in the nodes dict.
        """
        entry_address = self.workflow["entry"]
        if entry_address not in self.nodes:
            raise ValueError(
                f"Entry node '{entry_address}' not found in nodes. "
                f"Available nodes: {', '.join(sorted(self.nodes.keys()))}"
            )
        entry_freq = self._get_entry_frequency()

        # Build adjacency list and in-degree counts for topological sort
        outgoing: dict[str, list[dict]] = defaultdict(list)
        indegree: dict[str, int] = defaultdict(int)
        for edge in self.edges:
            outgoing[edge["from"]].append(edge)
            indegree[edge["to"]] += 1

        # Entry node gets full frequency; it has in-degree 0 by definition
        self.derived_usage[entry_address] = DerivedUsage(
            resource_address=entry_address,
            invocation_count=entry_freq,
            invocations_by_edge_type={DEFAULT_EDGE_TYPE: entry_freq},
        )

        # Topological sort (Kahn's algorithm): start with in-degree-zero nodes
        queue = [entry_address]
        while queue:
            node = queue.pop(0)
            parent_invocations = self.derived_usage[node].invocation_count

            for edge in outgoing.get(node, []):
                child = edge["to"]
                call_rate = self._resolve_value(edge["rate"])
                child_invocations = parent_invocations * call_rate

                # Accumulate data_in from edge dataSize
                data_bytes = 0.0
                data_size = edge.get("dataSize", {}) or edge.get("data_size", {})
                if data_size:
                    average = data_size.get("average", 0)
                    if average > 0:
                        data_bytes = parent_invocations * call_rate * average

                # Accumulate token flow from edge tokenFlow (DP#8)
                token_input = 0.0
                token_flow = edge.get("tokenFlow", {}) or edge.get("token_flow", {})
                if token_flow:
                    token_input = parent_invocations * call_rate * token_flow.get("input", 0)

                edge_type = edge.get("type", DEFAULT_EDGE_TYPE)

                if child in self.derived_usage:
                    self.derived_usage[child].invocation_count += child_invocations
                    self.derived_usage[child].data_in += data_bytes
                    self.derived_usage[child].input_tokens += token_input
                    self.derived_usage[child].edge_types.add(edge_type)
                else:
                    du = DerivedUsage(
                        resource_address=child,
                        invocation_count=child_invocations,
                        data_in=data_bytes,
                        input_tokens=token_input,
                    )
                    du.edge_types.add(edge_type)
                    self.derived_usage[child] = du
                by_type = self.derived_usage[child].invocations_by_edge_type
                by_type[edge_type] = by_type.get(edge_type, 0.0) + child_invocations

                indegree[child] -= 1
                # Only enqueue for downstream derivation when ALL incoming
                # edges have been processed, ensuring the accumulated
                # invocation_count is final and correct.
                if indegree[child] == 0:
                    queue.append(child)

        # Nodes defined but not reached by traversal from the entry node.
        unreached = set(self.nodes.keys()) - set(self.derived_usage.keys())

        # Always-on nodes carry frequency-independent (fixed) cost, so they are
        # costed without a synthetic incoming edge (Issue #196). Inject them
        # with zero derived traffic and exclude them from the unreachable
        # warning — their fixed cost is charged regardless of any flow.
        always_on = {
            addr for addr in unreached if _node_has_fixed_cost(self.nodes[addr])
        }
        for addr in always_on:
            self.derived_usage[addr] = DerivedUsage(
                resource_address=addr,
                invocation_count=0.0,
            )

        unreachable = sorted(unreached - always_on)
        if unreachable:
            warnings.warn(
                f"{len(unreachable)} node(s) are defined but unreachable from entry node "
                f"'{entry_address}' and will be excluded from cost: "
                f"{', '.join(unreachable)}"
            )

        return self.derived_usage

    def _get_entry_frequency(self) -> float:
        """Convert entry frequency to per-second rate."""
        freq = self.workflow["frequency"]
        value = freq["value"]
        unit = freq["unit"]

        # Convert to per-second (canonical unit)
        divisors = {
            "perSecond": 1.0,
            "perMinute": 60.0,  # per minute -> per second (divide)
            "perHour": 3600.0,  # per hour -> per second (divide)
            "perDay": 86400.0,  # per day -> per second (divide)
            "perWeek": 604800.0,  # per week -> per second (divide)
            "perMonth": 2629800.0,  # per month -> per second (divide)
        }

        if unit not in divisors:
            raise ValueError(
                f"Unknown frequency unit '{unit}'. "
                f"Valid units: {', '.join(sorted(divisors.keys()))}"
            )

        return value / divisors[unit]

    def _resolve_value(self, value) -> float:
        """Resolve a value that may be a parameter name or a numeric literal.

        Per DP#4, edge rates and usage metric values can reference symbolic
        parameters by name. If the value is a string, it is looked up in the
        parameters dict. If not found, it is treated as a float literal.

        Args:
            value: A numeric value or a parameter name string.

        Returns:
            Resolved float value.

        Raises:
            ValueError: If the value is a string that is not in parameters
                        and cannot be parsed as a float.
        """
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            if value in self.parameters:
                return self.parameters[value]
            try:
                return float(value)
            except ValueError:
                raise ValueError(
                    f"Unrecognized parameter reference '{value}'. "
                    f"Available parameters: {', '.join(sorted(self.parameters.keys()))}"
                ) from None
        return float(value)


@dataclass
class _CatalogCharge:
    """One catalog quantity that a node pays for.

    ``quantity`` and ``cost`` cover a month. ``cost`` is what the node's
    cost holds for this charge right now.
    """
    node: str
    pool: tuple
    quantity: float
    cost: float
    fixed: bool
    parameters: dict


def _pool_key(node: dict, metric: str, result) -> tuple:
    """The account-level pool that a catalog charge belongs to (#294).

    The provider applies tiers, such as a free allowance, to the account's
    total use of a metric in a region. A row with ``per`` scaling moves its
    boundaries by a parameter, so charges pool only when that parameter has
    the same value.
    """
    scaling = tuple(sorted(
        (tier.per, result.parameters.get(tier.per))
        for tier in result.tiers if tier.per
    ))
    return (node.get("provider"), node.get("service", ""), node.get("region"),
            metric, scaling)


def _price_pooled_charges(catalog: PricingCatalog,
                          charges: list[_CatalogCharge]) -> dict[str, list[float]]:
    """Price each pool's monthly total once and split it by quantity (#294).

    Each charge gets ``pool cost * charge quantity / pool quantity``, so the
    node costs still add up to the pool cost. Returns, per node, the change
    to its usage-driven cost (per second) and to its fixed cost (per
    month), and updates ``cost`` on each charge. A pool with one charge
    keeps its cost, unless it shares an account-wide free allowance with a
    pool in another region (#336), or its metric shares a free allowance
    with other metrics (#338), or its metric belongs to a global service
    used in another region (#378), or its region shares a meter or SKU with
    another pool's region (#389).
    """
    pools: dict[tuple, list[_CatalogCharge]] = defaultdict(list)
    for charge in charges:
        pools[charge.pool].append(charge)

    pool_costs = _price_account_wide_pools(catalog, pools)
    pool_costs.update(_price_shared_allowance_pools(catalog, pools))
    pool_costs.update(_price_global_pools(catalog, pools))
    pool_costs.update(_price_region_group_pools(catalog, pools))

    deltas: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for key, members in pools.items():
        (provider, service, region, metric, _) = key
        total_quantity = sum(c.quantity for c in members)
        if key in pool_costs:
            pool_cost = pool_costs[key]
        elif len(members) < 2 or total_quantity <= 0:
            continue
        else:
            pool_cost = catalog.query(
                provider, service, region, metric, total_quantity,
                parameters=members[0].parameters,
                period_seconds=SECONDS_PER_MONTH).total_cost
        for charge in members:
            share = pool_cost * charge.quantity / total_quantity
            delta = share - charge.cost
            charge.cost = share
            if charge.fixed:
                deltas[charge.node][1] += delta
            else:
                deltas[charge.node][0] += delta / SECONDS_PER_MONTH
    return deltas


def _price_account_wide_pools(catalog: PricingCatalog,
                              pools: dict[tuple, list[_CatalogCharge]]
                              ) -> dict[tuple, float]:
    """Share each account-wide free allowance across regions (#336).

    Some providers give a free allowance once to the account, across all
    regions. For each such metric used in more than one region, this applies
    the allowance once to the total quantity and gives each region a part of
    it in proportion to the region's quantity. Each region pays its own rate
    for the rest. Returns the monthly cost of each regional pool it priced.
    The pricing layer says which metrics are account-wide.
    """
    accounts: dict[tuple, list[tuple]] = defaultdict(list)
    for key, members in pools.items():
        provider, service, _, metric, scaling = key
        if shared_free_allowance(provider, service, metric) is not None:
            continue  # _price_shared_allowance_pools prices it (#338).
        if is_global_metric(provider, service, metric):
            continue  # _price_global_pools prices it (#378).
        if (free_tier_scope(provider, service, metric) == ACCOUNT
                and sum(c.quantity for c in members) > 0):
            accounts[(provider, service, metric, scaling)].append(key)

    costs: dict[tuple, float] = {}
    for keys in accounts.values():
        if len(keys) < 2:
            continue
        quantities = {k: sum(c.quantity for c in pools[k]) for k in keys}
        total = sum(quantities.values())
        results = {
            k: catalog.query(k[0], k[1], k[2], k[3], quantities[k],
                             parameters=pools[k][0].parameters,
                             period_seconds=SECONDS_PER_MONTH)
            for k in keys
        }
        if any(r is None for r in results.values()):
            continue
        # Regions normally state the same allowance. When they differ, use
        # the smallest so the account never gets more than any region states.
        allowance = min(r.free_allowance for r in results.values())
        free_fraction = min(1.0, allowance / total)
        for k in keys:
            paid = quantities[k] * (1.0 - free_fraction)
            costs[k] = catalog.query(
                k[0], k[1], k[2], k[3], paid,
                parameters=pools[k][0].parameters,
                include_free_tier=False,
                period_seconds=SECONDS_PER_MONTH).total_cost
    return costs


def _price_global_pools(catalog: PricingCatalog,
                        pools: dict[tuple, list[_CatalogCharge]]
                        ) -> dict[tuple, float]:
    """Price each global metric once across all regions (#378).

    A global service, such as Route 53, bills the account's use in every
    region together at one price. For each such metric used in more than one
    region, this prices the total quantity once and gives each regional pool
    a part of the cost in proportion to its quantity. The rows stored under
    ``GLOBAL_PRICE_REGIONS`` price the total, or else the rows of the pool's
    region that comes first in alphabetical order and has some, so the
    choice doesn't depend on node order. Returns the monthly cost of each
    regional pool it priced. The pricing layer says which metrics are global.
    """
    accounts: dict[tuple, list[tuple]] = defaultdict(list)
    for key, members in pools.items():
        provider, service, _, metric, scaling = key
        if (is_global_metric(provider, service, metric)
                and sum(c.quantity for c in members) > 0):
            accounts[(provider, service, metric, scaling)].append(key)

    costs: dict[tuple, float] = {}
    for (provider, service, metric, _), keys in accounts.items():
        if len(keys) < 2:
            continue
        quantities = {k: sum(c.quantity for c in pools[k]) for k in keys}
        total = sum(quantities.values())
        regions = list(GLOBAL_PRICE_REGIONS) + sorted(k[2] for k in keys)
        result = None
        for region in regions:
            result = catalog.query(provider, service, region, metric, total,
                                   parameters=pools[keys[0]][0].parameters,
                                   period_seconds=SECONDS_PER_MONTH)
            if result is not None:
                break
        if result is None:
            continue
        for k in keys:
            costs[k] = result.total_cost * quantities[k] / total
    return costs


def _price_region_group_pools(catalog: PricingCatalog,
                              pools: dict[tuple, list[_CatalogCharge]]
                              ) -> dict[tuple, float]:
    """Price each group of regions that share a meter or SKU once (#389).

    Azure bills internet egress on one meter for each zone, and GCP bills
    Cloud Run egress on one SKU for each continent. Each counts its tiers
    on the group's total. For each group with pools in more than one
    region, this prices the total quantity once and gives each regional
    pool a part of the cost in proportion to its quantity. The rows of the
    group's region that comes first in alphabetical order and has some
    price the total, so the choice doesn't depend on node order. Returns
    the monthly cost of each regional pool it priced. The pricing layer
    says which regions share a group.
    """
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for key, members in pools.items():
        provider, service, region, metric, scaling = key
        group = price_pool(provider, service, metric, region)
        if group is not None and sum(c.quantity for c in members) > 0:
            groups[(provider, service, metric, scaling, group)].append(key)

    costs: dict[tuple, float] = {}
    for (provider, service, metric, _, _), keys in groups.items():
        if len(keys) < 2:
            continue
        free_regions = FREE_ALLOWANCE_REGIONS.get((provider, service, metric))
        if free_regions is not None:
            costs.update(_price_partly_free_group(catalog, pools, keys, free_regions))
            continue
        quantities = {k: sum(c.quantity for c in pools[k]) for k in keys}
        total = sum(quantities.values())
        result = None
        for region in sorted(k[2] for k in keys):
            result = catalog.query(provider, service, region, metric, total,
                                   parameters=pools[keys[0]][0].parameters,
                                   period_seconds=SECONDS_PER_MONTH)
            if result is not None:
                break
        if result is None:
            continue
        for k in keys:
            costs[k] = result.total_cost * quantities[k] / total
    return costs


def _price_partly_free_group(catalog: PricingCatalog,
                             pools: dict[tuple, list[_CatalogCharge]],
                             keys: list[tuple], free_regions: tuple[str, ...]
                             ) -> dict[tuple, float]:
    """Price a group whose free allowance covers some regions only (#404).

    Cloud Storage bills egress from every region on one SKU, but gives its
    free 100 GiB to the use in three US regions only. The rows of those
    regions state the free tier, and their tier bounds count the free use
    too. This prices the group's total on the rows of the first of those
    regions in alphabetical order, or of the first region when the group
    has none of them. Where the free regions use less than the allowance,
    the rest of the allowance is paid at the first paid price. The cost is
    split by each pool's paid quantity, and a free region's pool gets a
    part of the free use in proportion to its quantity. Returns the monthly
    cost of each regional pool, or nothing when no region has rows.
    """
    provider, service, _, metric, _ = keys[0]
    quantities = {k: sum(c.quantity for c in pools[k]) for k in keys}
    total = sum(quantities.values())
    free_keys = [k for k in keys if k[2] in free_regions]
    result = None
    for region in (sorted(k[2] for k in free_keys)
                   + sorted(k[2] for k in keys if k not in free_keys)):
        result = catalog.query(provider, service, region, metric, total,
                               parameters=pools[keys[0]][0].parameters,
                               period_seconds=SECONDS_PER_MONTH)
        if result is not None:
            break
    if result is None:
        return {}
    free_quantity = sum(quantities[k] for k in free_keys)
    allowance = result.free_allowance
    free_used = min(allowance, free_quantity)
    cost = result.total_cost
    unused = min(allowance, total) - free_used
    if unused > 0:
        first_paid = next((t.price_usd for t in sorted(
            result.tiers, key=lambda t: t.start_usage_amount or 0)
            if t.price_usd > 0), 0.0)
        cost += unused * first_paid
    paid = {k: quantities[k] * (1 - free_used / free_quantity) if k in free_keys
            else quantities[k] for k in keys}
    paid_total = sum(paid.values())
    return {k: cost * paid[k] / paid_total if paid_total > 0 else 0.0
            for k in keys}


def _price_shared_allowance_pools(catalog: PricingCatalog,
                                  pools: dict[tuple, list[_CatalogCharge]]
                                  ) -> dict[tuple, float]:
    """Share one free allowance across several metrics (#338).

    Some providers give one free allowance to several metrics of a service,
    such as SQS standard and FIFO requests. For each such group, this
    applies the allowance once to the total quantity of all its metrics in
    all regions, and gives each pool a part of it in proportion to the
    pool's quantity. Each pool pays its own metric's rate, in its own
    region, for the rest, priced from the first paid tier. The allowance
    comes from the pricing layer's table, which overrides the free tiers of
    the metrics' rows. Returns the monthly cost of each pool it priced.
    """
    groups: dict[SharedFreeAllowance, list[tuple]] = defaultdict(list)
    for key, members in pools.items():
        provider, service, _, metric, _ = key
        group = shared_free_allowance(provider, service, metric)
        if group is not None and sum(c.quantity for c in members) > 0:
            groups[group].append(key)

    costs: dict[tuple, float] = {}
    for group, keys in groups.items():
        quantities = {k: sum(c.quantity for c in pools[k]) for k in keys}
        free_fraction = min(1.0, group.allowance / sum(quantities.values()))
        for k in keys:
            paid = quantities[k] * (1.0 - free_fraction)
            result = catalog.query(
                k[0], k[1], k[2], k[3], paid,
                parameters=pools[k][0].parameters,
                include_free_tier=False,
                period_seconds=SECONDS_PER_MONTH)
            if result is not None:
                costs[k] = result.total_cost
    return costs


@dataclass
class _ShapeCharge:
    """One usage-driven quantity of a shaped metric that a node pays for.

    ``quantity`` and ``cost`` cover a month. ``cost`` is what the node's
    usage-driven cost holds for this charge right now.
    """
    node: str
    metric: str
    params: dict
    quantity: float
    cost: float


def _price_pooled_shapes(charges: list[_ShapeCharge]) -> dict[str, float]:
    """Price each node's shaped metric once on a month of use (#305).

    A shape's parameters, such as a free allowance or a subscription rate,
    describe a month of the node's whole use. When several workflows reach
    a node, each one adds a charge for the same metric. This sums their
    monthly quantities, calls the handler once, and returns, per node, the
    change to its usage-driven cost (per second). A metric with one charge
    keeps its cost.
    """
    from infra_cost_model.saas import SaaSPricingRegistry

    pools: dict[tuple[str, str], list[_ShapeCharge]] = defaultdict(list)
    for charge in charges:
        pools[(charge.node, charge.metric)].append(charge)

    deltas: dict[str, float] = defaultdict(float)
    for (node, _), members in pools.items():
        if len(members) < 2:
            continue
        params = members[0].params
        quantity = sum(c.quantity for c in members)
        cost = SaaSPricingRegistry.compute(params["shape"], quantity, params)
        deltas[node] += (cost - sum(c.cost for c in members)) / SECONDS_PER_MONTH
    return deltas


class CostAggregator:
    """Aggregates costs bottom-up from derived usage + pricing."""

    def __init__(self, nodes: dict[str, dict], derived_usage: dict[str, DerivedUsage],
                 edges: list[dict] = None, catalog: Optional[PricingCatalog] = None,
                 parameters: dict[str, float] = None):
        self.nodes = nodes
        self.derived_usage = derived_usage
        self.edges = edges or []
        self.catalog = catalog
        self.parameters = parameters or {}
        self.costs: dict[str, float] = {}
        # Per-node fixed (frequency-independent) cost, expressed as a flat
        # monthly total. Tracked separately so the time-basis conversion scales
        # only the usage-driven portion of each node (Issue #196).
        self.fixed_costs: dict[str, float] = {}
        # Metrics with a non-zero quantity that no shape, catalog row or
        # pricingRates entry could price. Their cost is left out of the node.
        self.unpriced: list[_Miss] = []
        # Every catalog quantity a node pays for, so that tiers apply to the
        # account's total rather than to each node (#294).
        self.catalog_charges: list[_CatalogCharge] = []
        # Every usage-driven quantity of a shaped metric, so that a model
        # with several workflows can price each shape once (#305).
        self.shape_charges: list[_ShapeCharge] = []
        self._pricing_address: Optional[str] = None

    def _record_unpriced(self, address: str, node: dict, metric: str,
                         quantity: float, fixed: bool) -> None:
        if quantity == 0:
            return
        self.unpriced.append(_Miss(
            node=address, metric=metric, provider=node.get("provider"),
            service=node.get("service", ""), region=node.get("region"),
            quantity=quantity, fixed=fixed,
        ))

    def aggregate(self) -> dict[str, float]:
        """Aggregate costs. Returns combined (variable + fixed) node costs.

        Variable cost is in per-second internal units; fixed cost is a flat
        monthly total. The CostEngine converts each portion to the output
        time basis, using ``fixed_costs`` to tell them apart.

        Catalog tiers apply to the total across all nodes: each node first
        gets the cost of its own quantity, then the pooled pricing replaces
        it with the node's share of the pool cost (#294).
        """
        for addr, usage in self.derived_usage.items():
            if addr in self.nodes:
                self._pricing_address = addr
                variable, fixed = self._compute_node_cost(addr, usage)
                self.fixed_costs[addr] = fixed
                self.costs[addr] = variable + fixed
        self._pricing_address = None

        if self.catalog is not None:
            deltas = _price_pooled_charges(self.catalog, self.catalog_charges)
            for addr, (variable_delta, fixed_delta) in deltas.items():
                self.costs[addr] += variable_delta + fixed_delta
                self.fixed_costs[addr] += fixed_delta

        return self.costs

    def _compute_node_cost(self, address: str, usage: DerivedUsage) -> tuple[float, float]:
        """Compute the (variable, fixed) cost for a single node.

        Returns a tuple where:
        - variable cost is the usage-driven cost in per-second internal units
          (scaled later to the output time basis), and
        - fixed cost is the frequency-independent cost expressed as a flat
          monthly total (converted to the time basis by ``CostEngine``).

        A usage metric marked ``fixed: true`` — or any metric on a node with
        ``flatOverride: true`` — contributes to the fixed cost using its value
        directly (the escape hatch / always-on treatment of Principle 9 and
        Issue #196). All other metrics contribute to the variable cost, scaled
        by the derived invocation count.

        Pricing models handled: flat, tiered, token_based, percentage.
        """
        node = self.nodes.get(address, {})
        pricing_model = node.get("pricingModel", "flat")
        flat_override = node.get("flatOverride", False)

        # Warn only on the genuine flat-vs-derived conflict (DP#9): a node whose
        # cost is ENTIRELY fixed should not also receive DAG edges, since those
        # edges cannot influence its cost. A node mixing fixed and usage-driven
        # metrics legitimately consumes its incoming edges and does not warn.
        if _node_is_fully_fixed(node) and any(e.get("to") == address for e in self.edges):
            warnings.warn(
                f"Node '{address}' is fully fixed (flatOverride, or every usage "
                f"metric marked fixed) but also receives incoming DAG edges. Per "
                f"DP#9, flat overrides are an escape hatch and should not be "
                f"combined with DAG-derived usage. The DAG-derived invocation "
                f"count is being ignored for this node."
            )

        # Percentage and token pricing keep node-level flat/derived semantics:
        # the whole cost is fixed when flatOverride is set, else usage-driven.
        if pricing_model == "percentage":
            cost = self._compute_percentage_cost(address, node, usage.invocation_count)
            return (0.0, cost) if flat_override else (cost, 0.0)

        if pricing_model == "token_based":
            cost = self._compute_token_cost(address, node, usage)
            return (0.0, cost) if flat_override else (cost, 0.0)

        # Tiered pricing supports per-metric fixed flags like flat pricing.
        if pricing_model == "tiered":
            return self._compute_tiered_cost(address, node, usage)

        return self._compute_flat_cost(address, node, usage)

    def _compute_flat_cost(self, address: str, node: dict,
                           usage: DerivedUsage) -> tuple[float, float]:
        """Compute (variable, fixed) cost for flat-priced metrics.

        Each usageMetrics value is a per-invocation quantity multiplied by the
        derived invocation count, then by the pricing rate. Metrics marked fixed
        (or every metric when flatOverride is set) instead use their value
        directly as a flat monthly total. Catalog pricing is preferred over
        embedded pricingRates (Principle 13). Per DP#4, metric values may
        reference symbolic parameters by name.
        """
        node_metrics = node.get("usageMetrics", {})
        pricing_rates = node.get("pricingRates", {})
        flat_override = node.get("flatOverride", False)

        # A catalog lookup needs provider and region (DP#6). The rule is
        # shared with `validate` so both report the same nodes (#273).
        if self.catalog is not None:
            error = catalog_location_error(address, node)
            if error is not None:
                raise ValueError(error)

        variable_cost, consumed = self._price_derived_usage(address, node, usage)
        fixed_cost = 0.0
        for metric_name, metric_def in node_metrics.items():
            if metric_name in consumed:
                continue
            if isinstance(metric_def, dict):
                per_invocation = self._resolve_param(metric_def.get("value", 0))
            else:
                per_invocation = self._resolve_param(metric_def)

            metric_fixed = _metric_is_fixed(metric_def, flat_override)
            # Fixed metrics use their value directly; variable metrics scale by
            # the invocations they count (all, or one edge type's).
            total_quantity = (
                per_invocation if metric_fixed
                else usage.invocations_for(metric_def) * per_invocation
            )

            # SaaS pricing shapes (#241): if the metric declares a ``shape``,
            # dispatch to the pluggable SaaS pricing-handler registry before
            # the catalog / embedded-rates path. A shaped metric is priced by
            # its shape handler (flat_subscription, per_unit_flat, free_tier,
            # transactional, or a plugin-registered shape) using the metric's
            # inline parameters — this is the first-class path for non-IaC SaaS
            # resources that the catalog cannot reach. An unknown shape raises
            # ValueError from the registry rather than falling through to the
            # catalog, so a misspelled shape cannot price at $0.
            metric_cost = self._price_shape(metric_name, metric_def,
                                            total_quantity, metric_fixed)

            # Query catalog first (preferred path per Principle 13), else fall
            # back to embedded pricingRates (deprecated per Principle 13).
            if metric_cost is None and self.catalog is not None:
                result = self._query_catalog(node, metric_name,
                                             total_quantity, metric_fixed)
                if result is None:
                    # The node used a logical metric name (e.g. "natHours"); map it
                    # to the catalog usage_metric ("NAT-Gateway-Hour") via the
                    # owning handler and retry, so catalog pricing (live/seed) is
                    # reached instead of falling back to embedded pricingRates.
                    mapped = self._resolve_catalog_metric(address, node, metric_name)
                    if mapped is not None:
                        result = self._query_catalog(node, mapped,
                                                     total_quantity, metric_fixed)
                if result is not None:
                    metric_cost = result.total_cost
            if metric_cost is None and metric_name in pricing_rates:
                metric_cost = total_quantity * pricing_rates[metric_name]

            if metric_cost is None:
                self._record_unpriced(address, node, metric_name,
                                      total_quantity, metric_fixed)
                continue
            if metric_fixed:
                fixed_cost += metric_cost
            else:
                variable_cost += metric_cost

        return (variable_cost, fixed_cost)

    def _compute_tiered_cost(self, address: str, node: dict,
                             usage: DerivedUsage) -> tuple[float, float]:
        """Compute tiered pricing cost using the pricing catalog.

        Each usage metric represents a dimensional line item (e.g., storage GB,
        data transfer GB, request count). The total consumed quantity per metric
        is per_invocation_value × invocation_count. This quantity is used to
        query the catalog for tiered pricing, which includes free-tier handling
        (first N units at $0 before charging begins).

        Metrics marked fixed (or every metric when flatOverride is set) use
        their value directly as a flat monthly total instead of scaling by the
        invocation count, and are returned as the fixed portion (Issue #196).

        Falls back to flat pricingRates if the catalog is unavailable.
        """
        node_metrics = node.get("usageMetrics", {})
        pricing_rates = node.get("pricingRates", {})
        flat_override = node.get("flatOverride", False)

        # A catalog lookup needs provider and region (DP#6). The rule is
        # shared with `validate` so both report the same nodes (#273).
        if self.catalog is not None:
            error = catalog_location_error(address, node)
            if error is not None:
                raise ValueError(error)

        variable_cost, consumed = self._price_derived_usage(address, node, usage)
        fixed_cost = 0.0

        for metric_name, metric_def in node_metrics.items():
            if metric_name in consumed:
                continue
            if isinstance(metric_def, dict):
                per_invocation = self._resolve_param(metric_def.get("value", 0))
            else:
                per_invocation = self._resolve_param(metric_def)

            metric_fixed = _metric_is_fixed(metric_def, flat_override)
            total_quantity = (
                per_invocation if metric_fixed
                else usage.invocations_for(metric_def) * per_invocation
            )

            # SaaS pricing shapes (#241): dispatch to the shape registry before
            # the catalog path, same as _compute_flat_cost.
            metric_cost = self._price_shape(metric_name, metric_def,
                                            total_quantity, metric_fixed)

            if metric_cost is None and self.catalog is not None:
                result = self._query_catalog(node, metric_name,
                                             total_quantity, metric_fixed)
                if result is None:
                    # The node used a logical metric name (e.g. "natHours"); map it
                    # to the catalog usage_metric ("NAT-Gateway-Hour") via the
                    # owning handler and retry, so catalog pricing (live/seed) is
                    # reached instead of falling back to embedded pricingRates.
                    mapped = self._resolve_catalog_metric(address, node, metric_name)
                    if mapped is not None:
                        result = self._query_catalog(node, mapped,
                                                     total_quantity, metric_fixed)
                if result is not None:
                    metric_cost = result.total_cost
            # Fallback: flat pricingRates
            if metric_cost is None and metric_name in pricing_rates:
                metric_cost = total_quantity * pricing_rates[metric_name]

            if metric_cost is None:
                self._record_unpriced(address, node, metric_name,
                                      total_quantity, metric_fixed)
                continue
            if metric_fixed:
                fixed_cost += metric_cost
            else:
                variable_cost += metric_cost

        return (variable_cost, fixed_cost)

    def _price_derived_usage(self, address: str, node: dict,
                             derived_usage: DerivedUsage) -> tuple[float, frozenset]:
        """Price the catalog quantities the node's handler derives from
        several usage metrics, such as Lambda GB-seconds.

        Only usage-driven metrics without a ``shape`` feed the handler. Returns
        the usage-driven cost and the logical metrics it covers, which the
        caller then skips. Returns ``(0.0, frozenset())`` when there is no
        catalog, the handler derives nothing, or the catalog lacks a row for a
        derived quantity. The metrics then go through the per-metric path.

        Like every other catalog quantity, the derived quantities are priced
        against monthly tier boundaries, so the $0 free tier rows apply
        (#287).

        The handler works per node invocation. A metric that declares
        ``edgeType`` counts only some of those invocations, so its value is
        scaled by that share: when ``invocations`` counts only read calls,
        the requests and GB-seconds cover only those calls (#313).
        """
        from infra_cost_model.resources.registry import ResourceRegistry

        resource_address = node.get("resourceAddress") or address
        if self.catalog is None or not resource_address:
            return 0.0, frozenset()
        invocations = derived_usage.invocation_count
        usage = {}
        for name, metric_def in (node.get("usageMetrics") or {}).items():
            if _metric_is_fixed(metric_def, node.get("flatOverride", False)):
                continue
            share = (derived_usage.invocations_for(metric_def) / invocations
                     if invocations else 1.0)
            if isinstance(metric_def, dict):
                if metric_def.get("shape") is not None:
                    continue
                metric_def = metric_def.get("value", 0)
            usage[name] = self._resolve_param(metric_def) * share
        derived = ResourceRegistry.derive_catalog_usage(resource_address, usage)
        if derived is None:
            return 0.0, frozenset()

        cost = 0.0
        charges_before = len(self.catalog_charges)
        for catalog_metric, per_invocation in derived.quantities.items():
            result = self._query_catalog(node, catalog_metric,
                                         invocations * per_invocation, fixed=False)
            if result is None:
                del self.catalog_charges[charges_before:]
                return 0.0, frozenset()
            cost += result.total_cost
        return cost, derived.consumed

    def _price_shape(self, metric: str, metric_def, quantity: float,
                     fixed: bool) -> Optional[float]:
        """Price a metric through its SaaS pricing shape, if it has one.

        Returns ``None`` when the metric declares no ``shape``. Shape
        parameters, such as a subscription rate or a free allowance, describe
        a month. A fixed quantity is already a monthly total. A usage-driven
        quantity is a rate per second, so the handler gets a month of usage
        and its monthly cost is converted back to a cost per second, the way
        catalog tiers are priced (#292, #295). The handler also gets the
        metric's effective ``fixed`` flag, which ``flatOverride`` can set.

        Each usage-driven quantity is also kept as a charge, so that a model
        with several workflows can price the node's month of use once (#305).
        """
        if not isinstance(metric_def, dict) or metric_def.get("shape") is None:
            return None
        from infra_cost_model.saas import SaaSPricingRegistry
        period = 1.0 if fixed else SECONDS_PER_MONTH
        params = {**metric_def, "fixed": fixed}
        monthly_cost = SaaSPricingRegistry.compute(
            metric_def["shape"], quantity * period, params
        )
        if not fixed and self._pricing_address is not None:
            self.shape_charges.append(_ShapeCharge(
                node=self._pricing_address, metric=metric, params=params,
                quantity=quantity * period, cost=monthly_cost,
            ))
        return monthly_cost / period

    def _query_catalog(self, node: dict, metric: str, quantity: float,
                       fixed: bool):
        """Query the catalog for the cost of ``quantity`` of ``metric``.

        A usage-driven quantity is a rate per second, and a fixed quantity is
        a monthly total. The engine tells the catalog which period the
        quantity covers, and the catalog applies its tier boundaries to a
        month of usage (#287). The cost that comes back covers the same
        period as the quantity: per second, or per month for a fixed metric.

        Each priced quantity is also kept as a charge, so that ``aggregate``
        can apply the tiers to the account's total (#294).

        When the node's handler bills ``metric`` under another service, such
        as S3 egress under ``AWSDataTransfer``, the query and the pool use
        that service. The charge then shares one pool with the other nodes
        that pay for the same metric (#332).
        """
        node = self._node_for_metric(node, metric)
        regions = [node.get("region")]
        if is_global_metric(node.get("provider"), node.get("service", ""), metric):
            # A global service has one price everywhere, so a region with no
            # rows of its own uses the global or us-east-1 rows (#384).
            regions += [r for r in GLOBAL_PRICE_REGIONS if r != node.get("region")]
        result = None
        for region in regions:
            result = self.catalog.query(
                node.get("provider"), node.get("service", ""), region,
                metric, quantity, parameters=self.parameters,
                period_seconds=SECONDS_PER_MONTH if fixed else 1.0,
            )
            if result is not None:
                break
        if result is not None and self._pricing_address is not None:
            months = 1.0 if fixed else SECONDS_PER_MONTH
            self.catalog_charges.append(_CatalogCharge(
                node=self._pricing_address,
                pool=_pool_key(node, metric, result),
                quantity=quantity * months,
                cost=result.total_cost * months,
                fixed=fixed,
                parameters=self.parameters,
            ))
        return result

    def _node_for_metric(self, node: dict, metric: str) -> dict:
        """The node as the catalog sees it when it prices ``metric``: with
        the service the handler names for that metric, if any (#332)."""
        from infra_cost_model.resources.registry import ResourceRegistry

        resource_address = node.get("resourceAddress") or self._pricing_address
        if not resource_address:
            return node
        service = ResourceRegistry.resolve_catalog_service(resource_address, metric)
        if service is None or service == node.get("service"):
            return node
        return {**node, "service": service}

    def _resolve_catalog_metric(self, address: str, node: dict, logical_metric: str):
        """Translate a node's logical usageMetrics key to a catalog usage_metric
        name via the handler that owns the node's resource address.

        Returns the catalog name, or ``None`` when no handler matches the address
        or the handler declares no mapping for that logical name (in which case
        the caller falls back to embedded ``pricingRates``).
        """
        from infra_cost_model.resources.registry import ResourceRegistry

        resource_address = node.get("resourceAddress") or address
        if not resource_address:
            return None
        return ResourceRegistry.resolve_catalog_metric(resource_address, logical_metric)

    def _resolve_param(self, value) -> float:
        """Resolve a value that may be a parameter name or a numeric literal.

        Per DP#4, usage metric values can reference symbolic parameters by name.
        If the value is a string, it is looked up in the parameters dict.

        Args:
            value: A numeric value or a parameter name string.

        Returns:
            Resolved float value.

        Raises:
            ValueError: If the value is a string that is not in parameters
                        and cannot be parsed as a float.
        """
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            if value in self.parameters:
                return self.parameters[value]
            try:
                return float(value)
            except ValueError:
                raise ValueError(
                    f"Unrecognized parameter reference '{value}'. "
                    f"Available parameters: "
                    f"{', '.join(sorted(self.parameters.keys()))}"
                ) from None
        return float(value)

    def _compute_token_cost(self, address: str, node: dict, usage: DerivedUsage) -> float:
        """Compute token-based cost for LLM models (DP#8).

        Token pricing uses both invocation-derived token flow and node-level
        usage metrics. Input tokens flow from upstream edges; output and
        cached tokens are per-invocation node metrics.

        Recognized usageMetrics keys: inputTokens, outputTokens,
        cachedReadTokens, cacheWriteTokens.

        Uses catalog query if available (preferred per Principle 13),
        otherwise falls back to embedded pricingRates.
        """
        node_metrics = node.get("usageMetrics", {})
        pricing_rates = node.get("pricingRates", {})

        # Recognized token metric keys (Issue #194, #195)
        _RECOGNIZED_KEYS = {
            "inputTokens", "outputTokens", "cachedReadTokens", "cacheWriteTokens",
        }
        # Warn about unrecognized usageMetrics keys for token_based nodes (Issue #195)
        unrecognized = set(node_metrics.keys()) - _RECOGNIZED_KEYS
        if unrecognized:
            sorted_unrecognized = sorted(unrecognized)
            sorted_recognized = sorted(_RECOGNIZED_KEYS)
            warnings.warn(
                f"Node '{address}' has pricingModel 'token_based' with "
                f"unrecognized usageMetrics keys: "
                f"{', '.join(sorted_unrecognized)}. "
                f"Recognized keys for token_based pricing are: "
                f"{', '.join(sorted_recognized)}. "
                f"Unrecognized keys are ignored in cost computation."
            )

        # Total input tokens: from token flow distribution through edges,
        # with fallback to per-invocation node metric.
        total_input_tokens = usage.input_tokens
        if total_input_tokens == 0.0 and "inputTokens" in node_metrics:
            im = node_metrics["inputTokens"]
            input_per_call = self._resolve_param(
                im.get("value", 0) if isinstance(im, dict) else im
            )
            total_input_tokens = usage.invocation_count * input_per_call

        # Per-invocation token metrics (output and cached tokens).
        # These are NOT accumulated from edge tokenFlow; only inputTokens flows
        # through edges.
        def _resolve_per_invocation(key: str) -> float:
            if key not in node_metrics:
                return 0.0
            m = node_metrics[key]
            per_call = self._resolve_param(
                m.get("value", 0) if isinstance(m, dict) else m
            )
            return usage.invocation_count * per_call

        total_output_tokens = _resolve_per_invocation("outputTokens")
        total_cached_read_tokens = _resolve_per_invocation("cachedReadTokens")
        total_cache_write_tokens = _resolve_per_invocation("cacheWriteTokens")

        # Query each token class: catalog first (Principle 13), fallback to
        # embedded pricingRates.
        total_cost = 0.0
        token_classes = [
            ("inputTokens", total_input_tokens),
            ("outputTokens", total_output_tokens),
            ("cachedReadTokens", total_cached_read_tokens),
            ("cacheWriteTokens", total_cache_write_tokens),
        ]
        for token_name, total_tokens in token_classes:
            if total_tokens <= 0:
                continue
            if self.catalog is not None:
                fixed = node.get("flatOverride", False)
                result = self._query_catalog(node, token_name, total_tokens, fixed)
                if result is None:
                    # The catalog names token rows by provider, such as
                    # "Bedrock-Input-Token". The node's handler maps the
                    # logical token name to that row (#312).
                    mapped = self._resolve_catalog_metric(address, node, token_name)
                    if mapped is not None:
                        result = self._query_catalog(node, mapped, total_tokens, fixed)
                if result is not None:
                    total_cost += result.total_cost
                    continue
            if token_name in pricing_rates:
                total_cost += total_tokens * pricing_rates[token_name]
            else:
                self._record_unpriced(address, node, token_name, total_tokens,
                                      node.get("flatOverride", False))

        return total_cost

    def _compute_percentage_cost(self, address: str, node: dict, invocations: float) -> float:
        """Compute percentage-based cost for external services.

        For services like Stripe: 2.9% + $0.30 per transaction. The
        transactionVolume metric is the value of one transaction (Principle 4),
        so the cost is invocations × (volume × percentageRate +
        fixedPerTransaction).

        Args:
            address: Resource address
            node: Node configuration dict
            invocations: Transaction count

        Returns:
            Total cost from percentage pricing.
        """
        pricing_rates = node.get("pricingRates", {})

        # Default: percentage + fixed per transaction
        percentage_rate = pricing_rates.get("percentageRate", 0.0)
        fixed_per_tx = pricing_rates.get("fixedPerTransaction", 0.0)

        # Value of one transaction, from usageMetrics
        volume = 0.0
        usage_metrics = node.get("usageMetrics", {})
        for metric_name, metric_def in usage_metrics.items():
            if "volume" in metric_name.lower() or "transaction" in metric_name.lower():
                if isinstance(metric_def, dict):
                    volume = self._resolve_param(metric_def.get("value", 0))
                else:
                    volume = self._resolve_param(metric_def)
                break

        return invocations * (volume * percentage_rate + fixed_per_tx)


# SECONDS_PER_MONTH, the canonical time conversion, is imported from the
# pricing catalog, which also uses it as the tier boundary period (#287).


class CostEngine:
    """Main cost engine orchestrating derivation and aggregation."""

    def __init__(self, cost_model: dict, catalog: Optional[PricingCatalog] = None,
                 time_basis: str = "perSecond"):
        self.cost_model = cost_model
        self.workflow = cost_model.get("workflow")
        self.workflows = cost_model.get("workflows")
        self.nodes = cost_model["nodes"]
        self.edges = cost_model.get("edges", [])
        self.parameters = self.workflow.get("parameters", {}) if self.workflow else {}
        self.catalog = catalog
        self.time_basis = time_basis

        self.validator = DAGValidator(self.nodes, self.edges)
        self.derived_usage: dict[str, DerivedUsage] = {}
        self.costs: dict[str, float] = {}
        # Metrics left out of ``costs`` because nothing could price them.
        # Filled by ``compute``; each one also emits an UnpricedMetricWarning.
        self.unpriced_metrics: list[UnpricedMetric] = []
        # Metrics whose edgeType never reaches their node (#322). Filled by
        # ``compute``; each one also emits an EdgeTypeMetricWarning.
        self.edge_type_warnings: list[str] = []

    @property
    def _time_multiplier(self) -> float:
        """Multiplier to convert per-second costs to the output time basis."""
        if self.time_basis == "monthly":
            return SECONDS_PER_MONTH
        if self.time_basis == "yearly":
            return SECONDS_PER_MONTH * 12
        return 1.0  # perSecond

    @property
    def _fixed_multiplier(self) -> float:
        """Multiplier to convert monthly fixed costs to the output time basis.

        A fixed cost is a monthly total. Per second, it spreads over the
        seconds in a month, like every other monthly amount (#304).
        """
        if self.time_basis == "monthly":
            return 1.0
        if self.time_basis == "yearly":
            return 12.0
        return 1.0 / SECONDS_PER_MONTH  # perSecond

    def compute(self) -> dict[str, float]:
        """Run full cost derivation and aggregation.

        Supports both single-workflow and multi-workflow (workflows array)
        models. For multi-workflow, each workflow is derived independently
        and costs are aggregated across workflows.

        Returns:
            Dict mapping resource address to total cost.

        Raises:
            EngineRequirementError: If the model's `requiresEngine` does not
                hold for this engine. Refusing here covers every caller — the
                CLI, the SDK, and any script that builds an engine — so a
                model this engine cannot interpret never reports a total.
            ValueError: If DAG validation fails or if neither workflow
                        nor workflows is provided.
        """
        require_engine(self.cost_model)

        if self.workflows:
            return self._compute_multi_workflow()

        if self.workflow is None:
            raise ValueError(
                "Cost model must have either 'workflow' or 'workflows' field"
            )

        return self._compute_single_workflow()

    def _compute_single_workflow(self) -> dict[str, float]:
        """Compute costs for a single-workflow cost model."""
        if not self.validator.validate():
            raise ValueError(f"Invalid DAG: {'; '.join(self.validator.errors)}")

        deriver = WorkloadDeriver(self.workflow, self.nodes, self.edges,
                                   parameters=self.parameters)
        self.derived_usage = deriver.derive()

        aggregator = CostAggregator(self.nodes, self.derived_usage, self.edges,
                                     self.catalog, parameters=self.parameters)
        aggregator.aggregate()

        # Convert per-second usage-driven costs and monthly fixed costs to
        # the output time basis.
        self.costs = self._finalize_costs(aggregator.costs, aggregator.fixed_costs)
        self._report_unpriced(aggregator.unpriced)
        self._report_edge_types()

        return self.costs

    def _compute_multi_workflow(self) -> dict[str, float]:
        """Compute costs for a multi-workflow cost model.

        Each workflow is derived independently from its own entry point.
        Costs for shared nodes are summed across workflows. Catalog tiers and
        usage-driven SaaS shapes then apply to a month of use across all
        workflows, not to each workflow's share (#294, #305).
        """
        if not self.validator.validate():
            raise ValueError(f"Invalid DAG: {'; '.join(self.validator.errors)}")

        # Usage-driven costs accumulate across workflows; fixed (always-on)
        # costs are a property of the node and are counted exactly once.
        all_variable: dict[str, float] = defaultdict(float)
        all_fixed: dict[str, float] = {}
        all_derived: dict[str, DerivedUsage] = {}
        all_unpriced: list[_Miss] = []
        # Catalog charges from every workflow, so tiers apply to the account's
        # total once (#294). Like the fixed cost, a node's fixed charges come
        # from the last workflow that reaches it.
        variable_charges: list[_CatalogCharge] = []
        fixed_charges: dict[str, list[_CatalogCharge]] = {}
        # Usage-driven shaped metrics from every workflow, so that each
        # node's shape prices a month of its whole use once (#305).
        shape_charges: list[_ShapeCharge] = []

        for wf in self.workflows:
            wf_params = wf.get("parameters", {})
            deriver = WorkloadDeriver(wf, self.nodes, self.edges,
                                       parameters=wf_params)
            derived = deriver.derive()

            aggregator = CostAggregator(self.nodes, derived, self.edges,
                                         self.catalog, parameters=wf_params)
            aggregator.aggregate()
            all_unpriced.extend(aggregator.unpriced)
            shape_charges.extend(aggregator.shape_charges)

            wf_fixed_charges: dict[str, list[_CatalogCharge]] = defaultdict(list)
            for charge in aggregator.catalog_charges:
                if charge.fixed:
                    wf_fixed_charges[charge.node].append(charge)
                else:
                    variable_charges.append(charge)

            for addr, combined in aggregator.costs.items():
                fixed = aggregator.fixed_costs.get(addr, 0.0)
                all_variable[addr] += combined - fixed
                all_fixed[addr] = fixed
                fixed_charges[addr] = wf_fixed_charges.get(addr, [])

            # Merge derived usage (sum invocation counts for shared nodes)
            for addr, du in derived.items():
                if addr in all_derived:
                    all_derived[addr].invocation_count += du.invocation_count
                    all_derived[addr].data_in += du.data_in
                    all_derived[addr].input_tokens += du.input_tokens
                    all_derived[addr].edge_types |= du.edge_types
                    by_type = all_derived[addr].invocations_by_edge_type
                    for edge_type, count in du.invocations_by_edge_type.items():
                        by_type[edge_type] = by_type.get(edge_type, 0.0) + count
                else:
                    all_derived[addr] = du

        self.derived_usage = all_derived

        if self.catalog is not None:
            charges = variable_charges + [
                c for node_charges in fixed_charges.values() for c in node_charges
            ]
            deltas = _price_pooled_charges(self.catalog, charges)
            for addr, (variable_delta, fixed_delta) in deltas.items():
                all_variable[addr] += variable_delta
                all_fixed[addr] = all_fixed.get(addr, 0.0) + fixed_delta

        for addr, variable_delta in _price_pooled_shapes(shape_charges).items():
            all_variable[addr] += variable_delta

        # Convert both parts to the output time basis, the same way as
        # _finalize_costs, so the two workflow paths agree.
        multiplier = self._time_multiplier
        fixed_multiplier = self._fixed_multiplier
        self.costs = {}
        for addr in set(all_variable) | set(all_fixed):
            self.costs[addr] = (
                all_variable[addr] * multiplier
                + all_fixed.get(addr, 0.0) * fixed_multiplier
            )
        self._report_unpriced(all_unpriced)
        self._report_edge_types()

        return self.costs

    def _report_edge_types(self) -> None:
        """Store and warn about metrics whose edge type never reaches their node.

        The check runs on the derived usage of every workflow together, so a
        node that one workflow reads and another writes counts both types.
        """
        received = {
            address: {t for t, count in usage.invocations_by_edge_type.items() if count > 0}
            for address, usage in self.derived_usage.items()
        }
        self.edge_type_warnings = _edge_type_metric_warnings(self.nodes, received)
        for message in self.edge_type_warnings:
            warnings.warn(EdgeTypeMetricWarning(message), stacklevel=4)

    def _report_unpriced(self, misses: list["_Miss"]) -> None:
        """Store and warn about metrics that no price source covered.

        A metric is reported once per node, even when several workflows reach
        the node. Usage-driven quantities add up across workflows, and a fixed
        quantity counts once, matching how the costs combine.
        """
        merged: dict[tuple[str, str], _Miss] = {}
        for miss in misses:
            key = (miss.node, miss.metric)
            if key not in merged:
                merged[key] = _Miss(**asdict(miss))
            elif not miss.fixed:
                merged[key].quantity += miss.quantity

        multiplier = self._time_multiplier
        fixed_multiplier = self._fixed_multiplier
        self.unpriced_metrics = [
            UnpricedMetric(
                node=m.node, metric=m.metric, provider=m.provider,
                service=m.service, region=m.region,
                quantity=m.quantity * (fixed_multiplier if m.fixed else multiplier),
                time_basis=self.time_basis,
            )
            for m in merged.values()
        ]
        for unpriced in self.unpriced_metrics:
            warnings.warn(UnpricedMetricWarning(unpriced), stacklevel=4)

    def _finalize_costs(self, combined: dict[str, float],
                            fixed: dict[str, float]) -> dict[str, float]:
        """Convert each node's cost to the output time basis.

        ``combined[addr]`` is the usage-driven cost per second plus the fixed
        cost ``fixed[addr]``, which is a monthly total. Both parts convert to
        the output time basis, so the result has one unit: per second, per
        month or per year (Principle 9, #196, #304).
        """
        multiplier = self._time_multiplier
        fixed_multiplier = self._fixed_multiplier
        final: dict[str, float] = {}
        for addr, total in combined.items():
            fx = fixed.get(addr, 0.0)
            final[addr] = (total - fx) * multiplier + (fx * fixed_multiplier)
        return final

    def total_cost(self) -> float:
        """Get total system cost in the configured time basis."""
        if not self.costs:
            self.compute()
        return sum(self.costs.values())

    def get_derived_usage(self) -> dict[str, DerivedUsage]:
        """Get derived usage after computation."""
        if not self.derived_usage:
            self.compute()
        return self.derived_usage


class SensitivityAnalyzer:
    """What-if analysis and sensitivity analysis for cost models.

    Implements Principle 7: The model supports sensitivity analysis.
    """

    def __init__(self, cost_model: dict, catalog: Optional[PricingCatalog] = None,
                 time_basis: str = "perSecond"):
        self.cost_model = cost_model
        self.catalog = catalog
        self.time_basis = time_basis

    def what_if(self, parameter: str, value: float) -> float:
        """Run what-if analysis by varying a single parameter.

        Args:
            parameter: Parameter name (e.g., 'frequency', or edge rate like 'edge:from->to')
            value: New value for the parameter

        Returns:
            Total cost with the parameter change.
        """
        modified_model = self._modify_parameter(parameter, value)
        engine = CostEngine(modified_model, self.catalog, time_basis=self.time_basis)
        return engine.total_cost()

    def _modify_parameter(self, parameter: str, value: float) -> dict:
        """Create a modified cost model with parameter changed.

        Supports:
        - 'frequency': vary entry frequency
        - 'edge:from_node->to_node': vary a specific edge rate
        - Any name in workflow.parameters: vary a symbolic parameter (DP#4)
        """
        import copy
        model = copy.deepcopy(self.cost_model)

        if parameter == "frequency":
            model["workflow"]["frequency"]["value"] = value
        elif parameter.startswith("edge:"):
            # Format: edge:from_node->to_node
            edge_spec = parameter[5:]
            if "->" in edge_spec:
                from_node, to_node = edge_spec.split("->")
                for edge in model.get("edges", []):
                    if edge["from"] == from_node and edge["to"] == to_node:
                        edge["rate"] = value
                        break
        else:
            # Symbolic parameter (DP#4): update the parameter value in the
            # workflow's parameters dict. Edge rates and usage metrics that
            # reference this parameter by name will use the new value.
            params = model["workflow"].setdefault("parameters", {})
            params[parameter] = value

        return model

    def sensitivity(self, parameter: str, steps: int = 10) -> list[tuple[float, float]]:
        """Calculate cost sensitivity across parameter values.

        Args:
            parameter: Parameter to vary
            steps: Number of steps to evaluate

        Returns:
            List of (parameter_value, total_cost) tuples.
        """
        # Get baseline value
        if parameter == "frequency":
            baseline = self.cost_model["workflow"]["frequency"]["value"]
        elif parameter.startswith("edge:"):
            # Edge parameter: get current edge rate as baseline
            edge_spec = parameter[5:]
            baseline = 1.0  # fallback
            if "->" in edge_spec:
                from_node, to_node = edge_spec.split("->")
                for edge in self.cost_model.get("edges", []):
                    if edge["from"] == from_node and edge["to"] == to_node:
                        rate = edge["rate"]
                        baseline = float(rate) if isinstance(rate, (int, float)) else 1.0
                        break
        else:
            # Symbolic parameter (DP#4): get baseline from workflow.parameters
            params = self.cost_model["workflow"].get("parameters", {})
            baseline = params.get(parameter, 1.0)

        results = []
        # Vary from 0.5x to 2x baseline
        for i in range(steps):
            multiplier = 0.5 + (i * 1.5 / (steps - 1))  # 0.5 to 2.0
            value = baseline * multiplier
            cost = self.what_if(parameter, value)
            results.append((value, cost))

        return results

    def sweep_explicit(self, parameter: str, values: list[float]) -> list[dict]:
        """Run what-if sweep across explicit parameter values.

        Unlike sensitivity() which auto-generates a range, this evaluates
        exactly the values provided by the user — e.g., [1000, 10000, 100000].

        Returns per-node cost breakdowns for table/JSON output.

        Args:
            parameter: Parameter name (frequency, edge:from->to, or symbolic param).
            values: Explicit list of parameter values to evaluate.

        Returns:
            List of dicts, each with:
                - param_value: The parameter value evaluated
                - total_cost: Total system cost at this value
                - node_costs: Dict mapping node address to cost
        """
        results = []
        for value in values:
            modified = self._modify_parameter(parameter, value)
            engine = CostEngine(modified, self.catalog, time_basis=self.time_basis)
            node_costs = engine.compute()
            total = sum(node_costs.values())
            results.append({
                "param_value": value,
                "total_cost": total,
                "node_costs": dict(node_costs),
            })
        return results

    def sweep_compare(self, parameter: str, values: list[float],
                      other_model: dict) -> list[dict]:
        """Run what-if sweep against two models and compare costs.

        Evaluates the same parameter values against both self.cost_model
        and other_model, returning deltas for A/B architecture comparison.

        Args:
            parameter: Parameter name to vary.
            values: Explicit list of parameter values to evaluate.
            other_model: Second cost model to compare against.

        Returns:
            List of dicts, each with:
                - param_value: The parameter value evaluated
                - model_a: Dict with total_cost and node_costs for primary model
                - model_b: Dict with total_cost and node_costs for other model
                - delta: model_b.total_cost - model_a.total_cost
        """
        other_analyzer = SensitivityAnalyzer(other_model, self.catalog,
                                               time_basis=self.time_basis)
        results_a = self.sweep_explicit(parameter, values)
        results_b = other_analyzer.sweep_explicit(parameter, values)

        return [
            {
                "param_value": a["param_value"],
                "model_a": {"total_cost": a["total_cost"], "node_costs": a["node_costs"]},
                "model_b": {"total_cost": b["total_cost"], "node_costs": b["node_costs"]},
                "delta": b["total_cost"] - a["total_cost"],
            }
            for a, b in zip(results_a, results_b)
        ]

    def parameter_impact(self, parameter: str, delta: float = 0.1) -> float:
        """Calculate cost impact of a parameter change.

        Args:
            parameter: Parameter to vary. Supports:
                - "frequency": vary entry frequency
                - "edge:from_node->to_node": vary a specific edge rate
            delta: Fractional change (e.g., 0.1 = 10% change)

        Returns:
            Absolute cost difference.

        Raises:
            ValueError: If the parameter name is not supported.
        """
        engine = CostEngine(self.cost_model, self.catalog, time_basis=self.time_basis)
        baseline = engine.total_cost()

        if parameter == "frequency":
            current = self.cost_model["workflow"]["frequency"]["value"]
            new_value = current * (1 + delta)
            engine_modified = CostEngine(self._modify_parameter(parameter, new_value), self.catalog,
                                          time_basis=self.time_basis)
            return engine_modified.total_cost() - baseline

        if parameter.startswith("edge:"):
            # Validate edge format: edge:from_node->to_node
            edge_spec = parameter[5:]
            if "->" not in edge_spec:
                raise ValueError(
                    f"Unsupported parameter '{parameter}'. "
                    f"Edge parameters must use format 'edge:from_node->to_node'. "
                    f"Supported parameters: 'frequency', 'edge:from->to', "
                    f"or a workflow.parameters name."
                )
            from_node, to_node = edge_spec.split("->", 1)
            # Find the edge to get its current rate
            found = False
            for edge in self.cost_model.get("edges", []):
                if edge.get("from") == from_node and edge.get("to") == to_node:
                    current = edge.get("rate", 0.0)
                    if isinstance(current, str):
                        # Edge rate is a parameter reference — use parameter value
                        params = self.cost_model["workflow"].get("parameters", {})
                        current = params.get(current, 0.0)
                    found = True
                    break
            if not found:
                raise ValueError(
                    f"Edge '{from_node}->{to_node}' not found in cost model edges."
                )
            new_value = float(current) * (1 + delta)
            engine_modified = CostEngine(self._modify_parameter(parameter, new_value), self.catalog,
                                          time_basis=self.time_basis)
            return engine_modified.total_cost() - baseline

        # Symbolic parameter (DP#4): get current value from workflow.parameters
        params = self.cost_model["workflow"].get("parameters", {})
        if parameter in params:
            current = params[parameter]
            new_value = current * (1 + delta)
            engine_modified = CostEngine(self._modify_parameter(parameter, new_value), self.catalog,
                                          time_basis=self.time_basis)
            return engine_modified.total_cost() - baseline

        raise ValueError(
            f"Unsupported parameter '{parameter}'. "
            f"Supported parameters: 'frequency', 'edge:from_node->to_node', "
            f"or a workflow.parameters name."
        )


class ParametricSensitivityAnalyzer:
    """Efficient parametric sensitivity analysis for cost models.

    Implements Principle 7 with a parametric representation that avoids
    repeated full DAG re-derivation per data point. Supports:

    - Partial derivatives: d(Cost)/d(param) via analytic finite differences
    - Most impactful parameters: ranking by derivative magnitude
    - Multi-parameter what-if: applying multiple changes simultaneously
    - Parameter interaction: 2D sensitivity surfaces

    Unlike SensitivityAnalyzer which does copy.deepcopy + full engine
    re-derivation for every data point, this class:
    - Uses central finite differences (2 engine runs per derivative)
    - Supports ranking N parameters in O(N) engine runs, not O(N²)
    - Exposes interaction effects through multi-parameter surfaces
    """

    def __init__(self, cost_model: dict, catalog: Optional[PricingCatalog] = None,
                 time_basis: str = "perSecond"):
        self.cost_model = cost_model
        self.catalog = catalog
        self.time_basis = time_basis
        # Cache baseline for reuse
        self._baseline_engine: Optional[CostEngine] = None

    @property
    def baseline_cost(self) -> float:
        """Get or compute the baseline total cost."""
        if self._baseline_engine is None:
            self._baseline_engine = CostEngine(self.cost_model, self.catalog,
                                                 time_basis=self.time_basis)
        return self._baseline_engine.total_cost()

    def _get_parameter_value(self, parameter: str) -> float:
        """Get the current value of a parameter."""
        if parameter == "frequency":
            return self.cost_model["workflow"]["frequency"]["value"]
        if parameter.startswith("edge:"):
            edge_spec = parameter[5:]
            if "->" not in edge_spec:
                raise ValueError(f"Invalid edge parameter format: '{parameter}'. Use 'edge:from->to'.")
            from_node, to_node = edge_spec.split("->", 1)
            for edge in self.cost_model.get("edges", []):
                if edge.get("from") == from_node and edge.get("to") == to_node:
                    rate = edge.get("rate", 1.0)
                    if isinstance(rate, str):
                        params = self.cost_model["workflow"].get("parameters", {})
                        return params.get(rate, 0.0)
                    return float(rate)
            raise ValueError(f"Edge '{from_node}->{to_node}' not found in cost model edges.")
        # Symbolic parameter
        params = self.cost_model["workflow"].get("parameters", {})
        if parameter in params:
            return params[parameter]
        raise ValueError(
            f"Unknown parameter '{parameter}'. "
            f"Must be 'frequency', 'edge:from->to', or a workflow.parameters name."
        )

    def _modify_model(self, changes: dict[str, float]) -> dict:
        """Create a modified cost model with multiple parameter changes applied."""
        import copy
        model = copy.deepcopy(self.cost_model)
        for param, value in changes.items():
            if param == "frequency":
                model["workflow"]["frequency"]["value"] = value
            elif param.startswith("edge:"):
                edge_spec = param[5:]
                from_node, to_node = edge_spec.split("->", 1)
                for edge in model.get("edges", []):
                    if edge.get("from") == from_node and edge.get("to") == to_node:
                        edge["rate"] = value
                        break
            else:
                params = model["workflow"].setdefault("parameters", {})
                params[param] = value
        return model

    def partial_derivative(self, parameter: str, epsilon: float = None) -> float:
        """Compute the partial derivative of total cost with respect to a parameter.

        Uses central finite differences for accuracy: dC/dp ≈ (C(p+ε) - C(p-ε)) / (2ε).
        This requires exactly 2 engine runs regardless of model complexity.

        Args:
            parameter: Parameter name (frequency, edge:from->to, or symbolic param).
            epsilon: Perturbation size. Defaults to 0.1% of parameter value.

        Returns:
            Partial derivative ∂(Cost)/∂(parameter), in cost units per parameter unit.
            Positive means increasing the parameter increases cost; negative means
            increasing the parameter decreases cost.
        """
        baseline = self._get_parameter_value(parameter)
        if epsilon is None:
            epsilon = max(abs(baseline) * 0.001, 0.001)

        cost_plus = CostEngine(
            self._modify_model({parameter: baseline + epsilon}), self.catalog,
            time_basis=self.time_basis
        ).total_cost()
        cost_minus = CostEngine(
            self._modify_model({parameter: baseline - epsilon}), self.catalog,
            time_basis=self.time_basis
        ).total_cost()

        return (cost_plus - cost_minus) / (2 * epsilon)

    def most_impactful(self, parameters: list[str], top_n: int = None) -> list[dict]:
        """Identify which parameters have the greatest effect on total cost.

        Computes the partial derivative for each parameter and ranks by
        absolute impact magnitude. Unlike SensitivityAnalyzer.sensitivity()
        which sweeps a single parameter across 10+ points, this evaluates
        all parameters efficiently (2 engine runs each).

        Args:
            parameters: List of parameter names to evaluate.
            top_n: Return only the top N results (default: all).

        Returns:
            List of dicts sorted by |derivative| descending, each with:
                - parameter: Parameter name
                - derivative: Partial derivative value
                - abs_derivative: Absolute value of derivative
                - baseline_value: Current parameter value
                - elasticity: Percent change in cost per 1% change in param
        """
        results = []
        baseline = self.baseline_cost

        for param in parameters:
            param_value = self._get_parameter_value(param)
            deriv = self.partial_derivative(param)
            # Elasticity: (%Δ cost) / (%Δ param) = (dC/dp) * (p / C)
            elasticity = deriv * param_value / baseline if baseline != 0 else 0.0
            results.append({
                "parameter": param,
                "derivative": deriv,
                "abs_derivative": abs(deriv),
                "baseline_value": param_value,
                "elasticity": elasticity,
            })

        results.sort(key=lambda r: r["abs_derivative"], reverse=True)
        return results[:top_n] if top_n else results

    def multi_parameter_what_if(self, changes: dict[str, float]) -> float:
        """Evaluate cost with multiple simultaneous parameter changes.

        Unlike SensitivityAnalyzer.what_if() which varies one parameter at
        a time, this applies all changes in a single engine run, exposing
        interaction effects between parameters.

        Args:
            changes: Dict mapping parameter names to new values.

        Returns:
            Total cost with all parameter changes applied.
        """
        modified = self._modify_model(changes)
        return CostEngine(modified, self.catalog, time_basis=self.time_basis).total_cost()

    def parameter_sensitivity_surface(
        self, param1: str, param2: str, steps: int = 10
    ) -> list[dict]:
        """Compute a 2D sensitivity surface to expose interaction effects.

        Varies two parameters simultaneously across their range to reveal
        whether they interact (e.g., multiplicative effects) or are independent.

        Args:
            param1: First parameter name.
            param2: Second parameter name.
            steps: Number of steps in each dimension (grid is steps × steps).

        Returns:
            List of dicts with (param1_value, param2_value, total_cost) for
            each grid point, sorted by param1 then param2.
        """
        baseline1 = self._get_parameter_value(param1)
        baseline2 = self._get_parameter_value(param2)

        results = []
        for i in range(steps):
            mult1 = 0.5 + (i * 1.5 / max(steps - 1, 1))
            v1 = baseline1 * mult1
            for j in range(steps):
                mult2 = 0.5 + (j * 1.5 / max(steps - 1, 1))
                v2 = baseline2 * mult2
                cost = self.multi_parameter_what_if({param1: v1, param2: v2})
                results.append({
                    "param1": param1,
                    "param1_value": v1,
                    "param2": param2,
                    "param2_value": v2,
                    "total_cost": cost,
                })

        return results

    def what_if(self, parameter: str, value: float) -> float:
        """Convenience: single-parameter what-if (delegates to multi_parameter)."""
        return self.multi_parameter_what_if({parameter: value})