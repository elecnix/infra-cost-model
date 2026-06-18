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
                entry: str, frequency: Frequency,
                use_state_file: str = None) -> "Workflow":
        """Create workflow from Terraform infrastructure path.
        
        Auto-extracts resources from Terraform configuration and populates nodes.
        
        Args:
            name: Workflow identifier
            infra_path: Path to Terraform directory or state JSON file
            entry: Entry node resource address
            frequency: Entry invocation rate
            use_state_file: Optional path to terraform.tfstate.json file
            
        Returns:
            Workflow instance ready for calls definition.
            
        Raises:
            FileNotFoundError: If terraform not installed and no state file provided.
            RuntimeError: If terraform show fails.
        """
        workflow = cls(name)
        workflow.entry = entry
        workflow.frequency = frequency
        
        # Auto-extract nodes from Terraform
        if use_state_file:
            workflow._nodes = cls._extract_from_state(use_state_file)
        else:
            workflow._nodes = cls._extract_from_infra(infra_path)
        
        return workflow
    
    @classmethod
    def _extract_from_infra(cls, infra_path: str) -> dict[str, dict]:
        """Extract resources from Terraform directory using terraform show."""
        import json
        import subprocess
        from pathlib import Path
        
        infra_path = Path(infra_path)
        
        try:
            result = subprocess.run(
                ["terraform", "show", "-json"],
                cwd=infra_path,
                capture_output=True,
                text=True,
                check=True,
            )
            tf_json = json.loads(result.stdout)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"terraform show failed in {infra_path}: {e.stderr}"
            ) from e
        except FileNotFoundError as e:
            raise FileNotFoundError(
                "terraform not found. Install Terraform or use from_state_json()."
            ) from e
        
        return cls._extract_resources(tf_json)
    
    @classmethod
    def _extract_from_state(cls, state_path: str) -> dict[str, dict]:
        """Extract resources from Terraform state JSON file."""
        import json
        
        with open(state_path) as f:
            tf_json = json.load(f)
        
        return cls._extract_resources(tf_json)
    
    @classmethod
    def _extract_resources(cls, tf_json: dict) -> dict[str, dict]:
        """Extract resources from Terraform JSON using ResourceRegistry.
        
        Args:
            tf_json: Terraform show -json output or state JSON
            
        Returns:
            Dict mapping resource addresses to node configs.
        """
        from infra_cost_model.resources.registry import ResourceRegistry
        
        nodes = {}
        
        # Terraform show -json structure
        resources = tf_json.get("resource", []) or tf_json.get("values", {}).get(
            "root_module", {}
        ).get("resources", [])
        
        unsupported: list[str] = []
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            
            addr = resource.get("address")
            if not addr:
                continue
            
            extracted = ResourceRegistry.extract(addr, resource, "terraform")
            if extracted:
                nodes[addr] = extracted
            else:
                unsupported.append(addr)
        
        if unsupported:
            import warnings
            warnings.warn(
                f"{len(unsupported)} resource(s) could not be extracted because no "
                f"handler is registered for their resource type. Unsupported addresses: "
                f"{', '.join(sorted(unsupported))}. Consider adding a new ResourceType "
                f"handler for the unsupported resource(s)."
            )
        
        return nodes
    
    @classmethod
    def from_pulumi(cls, name: str, *,
                    entry: str, frequency: Frequency,
                    stack_name: str = None,
                    json_path: str = None) -> "Workflow":
        """Create workflow from Pulumi stack export.
        
        Auto-extracts resources from Pulumi stack and populates nodes.
        
        Args:
            name: Workflow identifier
            entry: Entry node resource address (Pulumi URN or logical name)
            frequency: Entry invocation rate
            stack_name: Pulumi stack name (e.g., 'dev'). If provided, runs
                        'pulumi stack export --json' in the current directory.
            json_path: Path to an existing pulumi stack export JSON file.
                       Takes precedence over stack_name.
            
        Returns:
            Workflow instance ready for calls definition.
            
        Raises:
            FileNotFoundError: If pulumi CLI not installed and no json_path.
            RuntimeError: If pulumi stack export fails.
        """
        workflow = cls(name)
        workflow.entry = entry
        workflow.frequency = frequency
        
        if json_path:
            import json
            with open(json_path) as f:
                pulumi_json = json.load(f)
        else:
            import json
            import subprocess
            try:
                cmd = ["pulumi", "stack", "export", "--json"]
                if stack_name:
                    cmd.extend(["--stack", stack_name])
                result = subprocess.run(
                    cmd, capture_output=True, text=True, check=True,
                )
                pulumi_json = json.loads(result.stdout)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"pulumi stack export failed: {e.stderr}"
                ) from e
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    "pulumi CLI not found. Install Pulumi or use json_path to "
                    "load an existing stack export JSON file."
                ) from e
        
        from infra_cost_model.resources.registry import extract_resources_from_pulumi
        workflow._nodes = extract_resources_from_pulumi(pulumi_json)
        return workflow
    
    @classmethod
    def from_cdk(cls, name: str, *,
                 entry: str, frequency: Frequency,
                 app_dir: str = None,
                 json_path: str = None) -> "Workflow":
        """Create workflow from CDK application.
        
        Auto-extracts resources from CDK-synthesized CloudFormation template.
        
        Args:
            name: Workflow identifier
            entry: Entry node resource address (CloudFormation logical ID or type:logical_id)
            frequency: Entry invocation rate
            app_dir: Path to CDK app directory. If provided, runs 'cdk synth --json'
                     in that directory.
            json_path: Path to an existing cdk.out/*.template.json file.
                       Takes precedence over app_dir.
            
        Returns:
            Workflow instance ready for calls definition.
            
        Raises:
            FileNotFoundError: If CDK CLI not installed and no json_path.
            RuntimeError: If cdk synth fails.
        """
        workflow = cls(name)
        workflow.entry = entry
        workflow.frequency = frequency
        
        if json_path:
            import json
            with open(json_path) as f:
                cdk_json = json.load(f)
        else:
            import json
            import subprocess
            try:
                cmd = ["cdk", "synth", "--json"]
                result = subprocess.run(
                    cmd,
                    cwd=app_dir or ".",
                    capture_output=True, text=True, check=True,
                )
                cdk_json = json.loads(result.stdout)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(
                    f"cdk synth failed: {e.stderr}"
                ) from e
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    "cdk CLI not found. Install AWS CDK or use json_path to "
                    "load an existing cdk.out template JSON file."
                ) from e
        
        from infra_cost_model.resources.registry import extract_resources_from_cdk
        workflow._nodes = extract_resources_from_cdk(cdk_json)
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
        workflow.parameters = model["workflow"].get("parameters", {})
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
    
    def parameter(self, name: str, value: float) -> "Workflow":
        """Set a symbolic parameter for what-if analysis (DP#4).
        
        Parameters are symbolic variables that can be varied for what-if
        analysis without re-deriving the graph structure. They can be
        referenced by name in edge rates and usage metric values.
        
        Args:
            name: Parameter name (e.g., 'cache_hit_rate')
            value: Parameter value
            
        Returns:
            Self for fluent chaining.
        """
        self.parameters[name] = value
        return self
    
    def to_cost_model(self) -> dict:
        """Export to cost model representation JSON Schema."""
        workflow_dict = {
            "name": self.name,
            "entry": self.entry,
            "frequency": {
                "unit": self.frequency.unit,
                "value": self.frequency.value,
            },
        }
        if self.parameters:
            workflow_dict["parameters"] = self.parameters
        return {
            "version": "1.0",
            "workflow": workflow_dict,
            "nodes": self._nodes,
            "edges": self._edges,
        }
    
    def validate(self) -> list[str]:
        """Validate this workflow against the JSON Schema."""
        return validate_cost_model(self.to_cost_model())