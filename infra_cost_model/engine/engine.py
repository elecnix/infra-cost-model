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
        """Traverse DAG top-down to compute derived usage for each node.
        
        Returns:
            Dict mapping resource address to DerivedUsage.
        """
        entry_address = self.workflow["entry"]
        entry_freq = self._get_entry_frequency()
        
        # Build adjacency list for traversal
        outgoing: dict[str, list[dict]] = defaultdict(list)
        for edge in self.edges:
            outgoing[edge["from"]].append(edge)
        
        # Entry node gets full frequency
        self.derived_usage[entry_address] = DerivedUsage(
            resource_address=entry_address,
            invocation_count=entry_freq,
        )
        
        # BFS traversal for derivation
        visited = {entry_address}
        queue = [entry_address]
        
        while queue:
            node = queue.pop(0)
            for edge in outgoing.get(node, []):
                child = edge["to"]
                if child not in visited:
                    visited.add(child)
                    queue.append(child)
                
                parent_invocations = self.derived_usage[node].invocation_count
                call_rate = edge["rate"]
                child_invocations = parent_invocations * call_rate
                
                if child in self.derived_usage:
                    self.derived_usage[child].invocation_count += child_invocations
                else:
                    self.derived_usage[child] = DerivedUsage(
                        resource_address=child,
                        invocation_count=child_invocations,
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
        }
        return value / divisors.get(unit, 1.0)


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
        """Compute direct cost for a single node."""
        node = self.nodes.get(address, {})
        node_metrics = node.get("usageMetrics", {})
        pricing_rates = node.get("pricingRates", {})
        
        total_cost = 0.0
        
        # Apply usage metrics with pricing rates
        for metric_name, metric_def in node_metrics.items():
            if isinstance(metric_def, dict):
                value = metric_def.get("value", 0)
            else:
                value = metric_def
            
            if metric_name in pricing_rates:
                total_cost += value * pricing_rates[metric_name]
        
        return total_cost


class CostEngine:
    """Main cost engine orchestrating derivation and aggregation."""
    
    def __init__(self, cost_model: dict, catalog: Optional[PricingCatalog] = None):
        self.cost_model = cost_model
        self.workflow = cost_model["workflow"]
        self.nodes = cost_model["nodes"]
        self.edges = cost_model.get("edges", [])
        self.catalog = catalog
        
        self.validator = DAGValidator(self.nodes, self.edges)
        self.derived_usage: dict[str, DerivedUsage] = {}
        self.costs: dict[str, float] = {}
    
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
        
        return self.costs
    
    def total_cost(self) -> float:
        """Get total system cost."""
        if not self.costs:
            self.compute()
        return sum(self.costs.values())
    
    def get_derived_usage(self) -> dict[str, DerivedUsage]:
        """Get derived usage after computation."""
        if not self.derived_usage:
            self.compute()
        return self.derived_usage