"""
Python SDK for cost model declaration.

This module provides a fluent API for declaring cost models, mirroring the YAML DSL.
Addresses Principle 11: Three surfaces (YAML, TypeScript, Python) share one schema.
"""

from dataclasses import dataclass, field
from typing import Optional, Union

import yaml

from infra_cost_model.schema.cost_model_schema import validate_cost_model


@dataclass
class Frequency:
    """Frequency unit for workflow entry rate."""
    value: float
    unit: str  # perSecond, perMinute, perHour, perDay


def per_second(value: float) -> Frequency:
    """Create per-second frequency."""
    return Frequency(value=value, unit="perSecond")


def per_minute(value: float) -> Frequency:
    """Create per-minute frequency."""
    return Frequency(value=value, unit="perMinute")


def per_hour(value: float) -> Frequency:
    """Create per-hour frequency."""
    return Frequency(value=value, unit="perHour")


def per_day(value: float) -> Frequency:
    """Create per-day frequency."""
    return Frequency(value=value, unit="perDay")


@dataclass
class Call:
    """Call edge definition in the DAG."""
    to: str
    rate: float
    type: str = "invoke"  # read, write, invoke
    data_size: Optional[dict] = None


@dataclass
class NodeUsage:
    """Usage metrics for a node."""
    metrics: dict[str, Union[float, dict]] = field(default_factory=dict)
    
    def with_metric(self, name: str, value: float, unit: Optional[str] = None) -> "NodeUsage":
        """Add a usage metric."""
        self.metrics[name] = {"value": value, "unit": unit} if unit else value
        return self


def parse_yaml_dsl(yaml_content: str) -> dict:
    """Parse YAML DSL with arrow syntax into cost model representation.
    
    Supports the DESIGN_PRINCIPLES.md DSL format:
    
    calls:
      aws_api_gatewayv2_api.llm_api:
        data_out: 50KB
        → aws_lambda_function.orchestrator: 1
    
    Also handles standard format with edges array.
    
    Args:
        yaml_content: YAML string with DSL format
        
    Returns:
        Cost model representation dict.
    """
    data = yaml.safe_load(yaml_content)
    
    if "workflow" not in data:
        raise ValueError("YAML must have 'workflow' section")
    
    # Handle shorthand frequency notation (e.g., "1000/min")
    workflow = data["workflow"]
    freq = workflow.get("frequency")
    if isinstance(freq, str):
        # Parse "1000/min" -> {"unit": "perMinute", "value": 1000}
        if "/" in freq:
            value, unit = freq.split("/")
            unit_map = {"sec": "perSecond", "min": "perMinute", "hr": "perHour", "day": "perDay"}
            workflow["frequency"] = {"value": float(value), "unit": unit_map.get(unit, "perMinute")}
    
    # Check if we have edges (standard format) or calls (DSL format)
    edges = data.get("edges", [])
    nodes = data.get("nodes", {})
    calls = data.get("calls", {})
    
    # Parse calls section with arrow syntax (DSL format)
    for source_addr, call_defs in calls.items():
        if not isinstance(call_defs, dict):
            continue
            
        for key, value in call_defs.items():
            # Arrow syntax: "→ aws_lambda_function.foo: 1" or "→ aws_lambda_function.foo: rate: 1"
            if key.startswith("→ ") or key.startswith("\u2192 "):
                target_addr = key[2:]
                if isinstance(value, (int, float)):
                    edges.append({"from": source_addr, "to": target_addr, "rate": float(value)})
                elif isinstance(value, dict):
                    edge = {"from": source_addr, "to": target_addr, "rate": value.get("rate", 1.0)}
                    if "type" in value:
                        edge["type"] = value["type"]
                    if "data_size" in value or "dataSize" in value:
                        edge["dataSize"] = value.get("dataSize", value.get("data_size"))
                    edges.append(edge)
            elif key == "data_out":
                # Keep data_out in nodes for reference
                if source_addr not in nodes:
                    nodes[source_addr] = {}
                nodes[source_addr]["dataOut"] = value
    
    return {
        "version": "1.0",
        "workflow": workflow,
        "nodes": nodes,
        "edges": edges,
    }


class Workflow:
    """Cost model workflow definition - main entry point for SDK."""
    
    def __init__(self, name: str):
        self.name = name
        self.entry: Optional[str] = None
        self.frequency: Optional[Frequency] = None
        self.parameters: dict[str, float] = {}
        self._nodes: dict[str, dict] = {}
        self._edges: list[dict] = []
    
    @classmethod
    def from_tf(cls, name: str, infra_path: str, *,
                entry: str, frequency: Frequency) -> "Workflow":
        """Create workflow from Terraform infrastructure path.
        
        Args:
            name: Workflow identifier
            infra_path: Path to Terraform files
            entry: Entry node resource address
            frequency: Entry invocation rate
            
        Returns:
            Workflow instance ready for calls definition.
        """
        # TODO: Auto-extract nodes from .tf files
        workflow = cls(name)
        workflow.entry = entry
        workflow.frequency = frequency
        return workflow
    
    @classmethod
    def from_yaml(cls, yaml_path: str) -> "Workflow":
        """Load workflow from YAML file with DSL parsing.
        
        Handles both standard schema format and DSL format with arrow syntax.
        """
        with open(yaml_path) as f:
            content = f.read()
        
        model = parse_yaml_dsl(content)
        
        # Validate against schema
        errors = validate_cost_model(model)
        if errors:
            raise ValueError(f"Invalid cost model: {errors}")
        
        workflow = cls(model["workflow"]["name"])
        workflow.entry = model["workflow"]["entry"]
        workflow.frequency = Frequency(
            value=model["workflow"]["frequency"]["value"],
            unit=model["workflow"]["frequency"]["unit"],
        )
        workflow._nodes = model.get("nodes", {})
        workflow._edges = model.get("edges", [])
        return workflow
    
    def calls(self, node_address: str, call_definitions: list[Call]) -> None:
        """Define outgoing edges from a node.
        
        Args:
            node_address: Source node resource address
            call_definitions: List of Call objects defining targets and rates
        """
        for call in call_definitions:
            edge = {
                "from": node_address,
                "to": call.to,
                "rate": call.rate,
                "type": call.type,
            }
            if call.data_size:
                edge["dataSize"] = call.data_size
            self._edges.append(edge)
    
    def usage(self, node_address: str, usage: NodeUsage) -> None:
        """Set usage metrics for a node.
        
        Args:
            node_address: Target node resource address
            usage: NodeUsage with metrics
        """
        if node_address not in self._nodes:
            self._nodes[node_address] = {}
        self._nodes[node_address]["usageMetrics"] = usage.metrics
    
    def to_cost_model(self) -> dict:
        """Export to cost model representation JSON Schema."""
        return {
            "version": "1.0",
            "workflow": {
                "name": self.name,
                "entry": self.entry,
                "frequency": {
                    "unit": self.frequency.unit,
                    "value": self.frequency.value,
                },
            },
            "nodes": self._nodes,
            "edges": self._edges,
        }
    
    def validate(self) -> list[str]:
        """Validate this workflow against the JSON Schema."""
        return validate_cost_model(self.to_cost_model())