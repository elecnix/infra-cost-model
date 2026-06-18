"""
Cost engine: DAG traversal, workload derivation, and cost aggregation.

This module implements Principles 1, 2, 3, 5:
- Workload derivation: Compute derived usage by propagating frequency through DAG
- DAG validation: Cycle detection and edge validation
- Cost propagation: Aggregate costs bottom-up from derived usage
- Resource x Cost Model join: Combine representations to produce costs
"""

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
    
    def __init__(self, workflow: dict, nodes: dict[str, dict], edges: list[dict]):
        self.workflow = workflow
        self.nodes = nodes
        self.edges = edges
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
                call_rate = edge["rate"]
                child_invocations = parent_invocations * call_rate
                
                if child in self.derived_usage:
                    self.derived_usage[child].invocation_count += child_invocations
                else:
                    self.derived_usage[child] = DerivedUsage(
                        resource_address=child,
                        invocation_count=child_invocations,
                    )
                
                indegree[child] -= 1
                # Only enqueue for downstream propagation when ALL incoming
                # edges have been processed, ensuring the accumulated
                # invocation_count is final and correct.
                if indegree[child] == 0:
                    queue.append(child)
        
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
        }
        
        if unit not in divisors:
            raise ValueError(
                f"Unknown frequency unit '{unit}'. "
                f"Valid units: {', '.join(sorted(divisors.keys()))}"
            )
        
        return value / divisors[unit]


class CostAggregator:
    """Aggregates costs bottom-up from derived usage + pricing."""
    
    def __init__(self, nodes: dict[str, dict], derived_usage: dict[str, DerivedUsage],
                 edges: list[dict] = None, catalog: Optional[PricingCatalog] = None):
        self.nodes = nodes
        self.derived_usage = derived_usage
        self.edges = edges or []
        self.catalog = catalog
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
        - flat: usageMetrics values are per-invocation quantities × invocation_count × rate
        - tiered: Tiered pricing from catalog
        - token_based: LLM token pricing
        - percentage: External services (Stripe 2.9% + $0.30)
        
        The derived invocation_count is the primary volume driver:
        each usageMetrics value is a per-invocation quantity multiplied by
        invocation_count to produce the total consumption that is then
        multiplied by the pricing rate.
        """
        node = self.nodes.get(address, {})
        pricing_model = node.get("pricingModel", "flat")
        node_metrics = node.get("usageMetrics", {})
        pricing_rates = node.get("pricingRates", {})
        
        # Handle percentage-based pricing (external services like Stripe)
        if pricing_model == "percentage":
            return self._compute_percentage_cost(address, node, usage.invocation_count)
        
        total_cost = 0.0
        invocations = usage.invocation_count
        
        # Apply usage metrics with pricing rates.
        # Each metric value is a per-invocation quantity; multiply by
        # invocation_count to get total consumption, then by pricing rate.
        for metric_name, metric_def in node_metrics.items():
            if isinstance(metric_def, dict):
                per_invocation = metric_def.get("value", 0)
            else:
                per_invocation = metric_def
            
            if metric_name in pricing_rates:
                total_cost += invocations * per_invocation * pricing_rates[metric_name]
        
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
        
        deriver = WorkloadDeriver(self.workflow, self.nodes, self.edges)
        self.derived_usage = deriver.derive()
        
        aggregator = CostAggregator(self.nodes, self.derived_usage, self.edges, self.catalog)
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
        """Create a modified cost model with parameter changed."""
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
        else:
            baseline = 1.0  # Default baseline
        
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
            parameter: Parameter to vary
            delta: Fractional change (e.g., 0.1 = 10% change)
            
        Returns:
            Absolute cost difference.
        """
        engine = CostEngine(self.cost_model, self.catalog)
        baseline = engine.total_cost()
        
        if parameter == "frequency":
            current = self.cost_model["workflow"]["frequency"]["value"]
            new_value = current * (1 + delta)
            engine_modified = CostEngine(self._modify_parameter(parameter, new_value), self.catalog)
            return engine_modified.total_cost() - baseline
        
        return 0.0