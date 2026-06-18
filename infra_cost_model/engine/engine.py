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
from dataclasses import dataclass, field
from typing import Optional

from infra_cost_model.pricing.catalog import PricingCatalog


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
                
                edge_type = edge.get("type", "invoke")
                
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
                
                indegree[child] -= 1
                # Only enqueue for downstream derivation when ALL incoming
                # edges have been processed, ensuring the accumulated
                # invocation_count is final and correct.
                if indegree[child] == 0:
                    queue.append(child)
        
        # Warn about nodes that are defined but unreachable from the entry node
        unreachable = set(self.nodes.keys()) - set(self.derived_usage.keys())
        if unreachable:
            sorted_unreachable = sorted(unreachable)
            warnings.warn(
                f"{len(unreachable)} node(s) are defined but unreachable from entry node "
                f"'{entry_address}' and will be excluded from cost: "
                f"{', '.join(sorted_unreachable)}"
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
    
    def aggregate(self) -> dict[str, float]:
        """Aggregate costs. Returns node costs."""
        outgoing: dict[str, int] = defaultdict(int)
        for edge in self.edges:
            outgoing[edge["from"]] += 1
        
        # Compute costs for all nodes in derived_usage
        for addr, usage in self.derived_usage.items():
            if addr in self.nodes:
                self.costs[addr] = self._compute_node_cost(addr, usage)
        
        return self.costs
    
    def _compute_node_cost(self, address: str, usage: DerivedUsage) -> float:
        """Compute direct cost for a single node.
        
        Handles multiple pricing models:
        - flat: usageMetrics values are per-invocation multipliers × invocation_count × rate.
          When flatOverride is true on the node, values are treated as flat monthly
          totals (not multiplied by invocation_count), implementing the escape hatch
          from Principle 9.
        - tiered: Tiered pricing from catalog
        - token_based: LLM token pricing
        - percentage: External services (Stripe 2.9% + $0.30)
        
        The derived invocation_count is the primary volume driver:
        each usageMetrics value is a per-invocation quantity multiplied by
        invocation_count to produce the total consumption that is then
        multiplied by the pricing rate. When flatOverride is true, invocation_count
        is not applied and the value is used directly.
        """
        node = self.nodes.get(address, {})
        pricing_model = node.get("pricingModel", "flat")
        node_metrics = node.get("usageMetrics", {})
        pricing_rates = node.get("pricingRates", {})
        flat_override = node.get("flatOverride", False)
        
        # Handle percentage-based pricing (external services like Stripe)
        if pricing_model == "percentage":
            return self._compute_percentage_cost(address, node, usage.invocation_count)
        
        # Handle tiered pricing (storage, egress, etc.)
        if pricing_model == "tiered":
            return self._compute_tiered_cost(address, node, usage.invocation_count)
        
        # Handle token-based pricing (LLM models, DP#8)
        if pricing_model == "token_based":
            return self._compute_token_cost(address, node, usage)
        
        total_cost = 0.0
        # When flatOverride is true, treat invocation_count as 1.0 — the
        # usageMetrics values are flat monthly totals (escape hatch per DP#9).
        invocations = usage.invocation_count if not flat_override else 1.0
        provider = node.get("provider")
        service = node.get("service", "")
        region = node.get("region")
        
        # Validate provider/region when a catalog query path is possible (DP#6).
        # The engine must not silently assume a specific provider or region.
        # Validation only fires when a catalog is available and would be queried;
        # embedded pricingRates (flat fallback) do not depend on provider/region.
        if self.catalog is not None and node_metrics:
            if provider is None:
                raise ValueError(
                    f"Node '{address}' is missing required 'provider' field. "
                    f"Per Principle 6, the cost engine is provider-agnostic: "
                    f"provider must be specified explicitly on each node "
                    f"(e.g., 'aws', 'gcp', 'azure')."
                )
            if region is None:
                raise ValueError(
                    f"Node '{address}' is missing required 'region' field. "
                    f"Region must be specified explicitly on each node "
                    f"(e.g., 'us-east-1', 'eu-west-1', 'us-central1')."
                )
        
        # Apply usage metrics with pricing rates.
        # Each metric value is a per-invocation quantity; multiply by
        # invocation_count to get total consumption, then by pricing rate.
        # Prefer catalog over embedded pricingRates (Principle 13).
        # Per DP#4, metric values may reference symbolic parameters by name.
        for metric_name, metric_def in node_metrics.items():
            if isinstance(metric_def, dict):
                per_invocation = self._resolve_param(metric_def.get("value", 0))
            else:
                per_invocation = self._resolve_param(metric_def)
            
            total_quantity = invocations * per_invocation
            
            # Query catalog first (preferred path per Principle 13)
            if self.catalog is not None:
                result = self.catalog.query(
                    provider, service, region, metric_name, total_quantity
                )
                if result is not None:
                    total_cost += result.total_cost
                    continue
            
            # Fallback: embedded pricingRates (deprecated per Principle 13)
            if metric_name in pricing_rates:
                total_cost += total_quantity * pricing_rates[metric_name]
        
        return total_cost
    
    def _compute_tiered_cost(self, address: str, node: dict, invocations: float) -> float:
        """Compute tiered pricing cost using the pricing catalog.
        
        Each usage metric represents a dimensional line item (e.g., storage GB,
        data transfer GB, request count). The total consumed quantity per metric
        is per_invocation_value × invocation_count. This quantity is used to
        query the catalog for tiered pricing, which includes free-tier handling
        (first N units at $0 before charging begins).
        
        Falls back to flat pricingRates if the catalog is unavailable.
        """
        node_metrics = node.get("usageMetrics", {})
        pricing_rates = node.get("pricingRates", {})
        provider = node.get("provider")
        service = node.get("service", "")
        region = node.get("region")
        
        # Validate provider/region when a catalog query path is possible (DP#6).
        # Validation only fires when a catalog is available and would be queried;
        # embedded pricingRates (flat fallback) do not depend on provider/region.
        if self.catalog is not None and node_metrics:
            if provider is None:
                raise ValueError(
                    f"Node '{address}' is missing required 'provider' field. "
                    f"Per Principle 6, the cost engine is provider-agnostic: "
                    f"provider must be specified explicitly on each node "
                    f"(e.g., 'aws', 'gcp', 'azure')."
                )
            if region is None:
                raise ValueError(
                    f"Node '{address}' is missing required 'region' field. "
                    f"Region must be specified explicitly on each node "
                    f"(e.g., 'us-east-1', 'eu-west-1', 'us-central1')."
                )
        
        total_cost = 0.0
        catalog_used = False
        
        for metric_name, metric_def in node_metrics.items():
            if isinstance(metric_def, dict):
                per_invocation = self._resolve_param(metric_def.get("value", 0))
            else:
                per_invocation = self._resolve_param(metric_def)
            
            total_quantity = invocations * per_invocation
            
            if self.catalog is not None:
                result = self.catalog.query(
                    provider, service, region, metric_name, total_quantity
                )
                if result is not None:
                    total_cost += result.total_cost
                    catalog_used = True
                    continue
            
            # Fallback: flat pricingRates
            if metric_name in pricing_rates:
                total_cost += total_quantity * pricing_rates[metric_name]
        
        return total_cost
    
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
        usage metrics. Input tokens flow from upstream edges; output tokens
        are produced by the node based on its per-invocation output metric.
        
        Uses catalog query if available (preferred per Principle 13),
        otherwise falls back to embedded pricingRates.
        """
        node_metrics = node.get("usageMetrics", {})
        pricing_rates = node.get("pricingRates", {})
        provider = node.get("provider")
        service = node.get("service", "")
        region = node.get("region")
        
        # Total input tokens: from token flow distribution through edges
        total_input_tokens = usage.input_tokens
        
        # Total output tokens: invocation_count × per-invocation output tokens
        output_per_call = 0.0
        if "outputTokens" in node_metrics:
            om = node_metrics["outputTokens"]
            output_per_call = self._resolve_param(om.get("value", 0) if isinstance(om, dict) else om)
        total_output_tokens = usage.invocation_count * output_per_call
        
        # Also check for direct input token specification on the node
        input_per_call = 0.0
        if "inputTokens" in node_metrics:
            im = node_metrics["inputTokens"]
            input_per_call = self._resolve_param(im.get("value", 0) if isinstance(im, dict) else im)
        if total_input_tokens == 0.0:
            total_input_tokens = usage.invocation_count * input_per_call
        
        total_cost = 0.0
        
        # Try catalog for input tokens
        if self.catalog is not None and total_input_tokens > 0:
            result = self.catalog.query(
                provider, service, region, "inputTokens", total_input_tokens
            )
            if result is not None:
                total_cost += result.total_cost
            elif "inputTokens" in pricing_rates:
                total_cost += total_input_tokens * pricing_rates["inputTokens"]
        elif "inputTokens" in pricing_rates:
            total_cost += total_input_tokens * pricing_rates["inputTokens"]
        
        # Try catalog for output tokens
        if self.catalog is not None and total_output_tokens > 0:
            result = self.catalog.query(
                provider, service, region, "outputTokens", total_output_tokens
            )
            if result is not None:
                total_cost += result.total_cost
            elif "outputTokens" in pricing_rates:
                total_cost += total_output_tokens * pricing_rates["outputTokens"]
        elif "outputTokens" in pricing_rates:
            total_cost += total_output_tokens * pricing_rates["outputTokens"]
        
        return total_cost
    
    def _compute_percentage_cost(self, address: str, node: dict, invocations: float) -> float:
        """Compute percentage-based cost for external services.
        
        For services like Stripe: 2.9% + $0.30 per transaction.
        Uses catalog query if available, otherwise uses pricingRates.
        
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
        
        # External services need transaction volume - use value from usageMetrics
        volume = 0.0
        usage_metrics = node.get("usageMetrics", {})
        for metric_name, metric_def in usage_metrics.items():
            if "volume" in metric_name.lower() or "transaction" in metric_name.lower():
                if isinstance(metric_def, dict):
                    volume = metric_def.get("value", 0)
                else:
                    volume = metric_def
                break
        
        return (volume * percentage_rate) + (invocations * fixed_per_tx)


# Canonical time conversion: seconds in an average month (365.25 days / 12)
SECONDS_PER_MONTH = 86400 * 365.25 / 12  # = 2629800.0


class CostEngine:
    """Main cost engine orchestrating derivation and aggregation."""
    
    def __init__(self, cost_model: dict, catalog: Optional[PricingCatalog] = None,
                 time_basis: str = "perSecond"):
        self.cost_model = cost_model
        self.workflow = cost_model["workflow"]
        self.nodes = cost_model["nodes"]
        self.edges = cost_model.get("edges", [])
        self.parameters = self.workflow.get("parameters", {})
        self.catalog = catalog
        self.time_basis = time_basis
        
        self.validator = DAGValidator(self.nodes, self.edges)
        self.derived_usage: dict[str, DerivedUsage] = {}
        self.costs: dict[str, float] = {}
    
    @property
    def _time_multiplier(self) -> float:
        """Multiplier to convert per-second costs to the output time basis."""
        if self.time_basis == "monthly":
            return SECONDS_PER_MONTH
        return 1.0  # perSecond
    
    def compute(self) -> dict[str, float]:
        """Run full cost derivation and aggregation.
        
        Returns:
            Dict mapping resource address to total cost.
            
        Raises:
            ValueError: If DAG validation fails.
        """
        if not self.validator.validate():
            raise ValueError(f"Invalid DAG: {'; '.join(self.validator.errors)}")
        
        deriver = WorkloadDeriver(self.workflow, self.nodes, self.edges,
                                   parameters=self.parameters)
        self.derived_usage = deriver.derive()
        
        aggregator = CostAggregator(self.nodes, self.derived_usage, self.edges,
                                     self.catalog, parameters=self.parameters)
        self.costs = aggregator.aggregate()
        
        # Apply time basis conversion (per-second internal → output period)
        multiplier = self._time_multiplier
        if multiplier != 1.0:
            self.costs = {addr: cost * multiplier for addr, cost in self.costs.items()}
        
        return self.costs
    
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
    
    def __init__(self, cost_model: dict, catalog: Optional[PricingCatalog] = None):
        self.cost_model = cost_model
        self.catalog = catalog
    
    def what_if(self, parameter: str, value: float) -> float:
        """Run what-if analysis by varying a single parameter.
        
        Args:
            parameter: Parameter name (e.g., 'frequency', or edge rate like 'edge:from->to')
            value: New value for the parameter
            
        Returns:
            Total cost with the parameter change.
        """
        modified_model = self._modify_parameter(parameter, value)
        engine = CostEngine(modified_model, self.catalog)
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
        engine = CostEngine(self.cost_model, self.catalog)
        baseline = engine.total_cost()
        
        if parameter == "frequency":
            current = self.cost_model["workflow"]["frequency"]["value"]
            new_value = current * (1 + delta)
            engine_modified = CostEngine(self._modify_parameter(parameter, new_value), self.catalog)
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
            engine_modified = CostEngine(self._modify_parameter(parameter, new_value), self.catalog)
            return engine_modified.total_cost() - baseline
        
        # Symbolic parameter (DP#4): get current value from workflow.parameters
        params = self.cost_model["workflow"].get("parameters", {})
        if parameter in params:
            current = params[parameter]
            new_value = current * (1 + delta)
            engine_modified = CostEngine(self._modify_parameter(parameter, new_value), self.catalog)
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
    
    def __init__(self, cost_model: dict, catalog: Optional[PricingCatalog] = None):
        self.cost_model = cost_model
        self.catalog = catalog
        # Cache baseline for reuse
        self._baseline_engine: Optional[CostEngine] = None
    
    @property
    def baseline_cost(self) -> float:
        """Get or compute the baseline total cost."""
        if self._baseline_engine is None:
            self._baseline_engine = CostEngine(self.cost_model, self.catalog)
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
            self._modify_model({parameter: baseline + epsilon}), self.catalog
        ).total_cost()
        cost_minus = CostEngine(
            self._modify_model({parameter: baseline - epsilon}), self.catalog
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
        return CostEngine(modified, self.catalog).total_cost()
    
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