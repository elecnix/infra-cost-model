"""
CLI entry point for infra-cost-model.

Provides commands for cost model validation, computation, and analysis.
"""

import sys
import json
from pathlib import Path
from typing import Optional

import yaml

from infra_cost_model.schema import validate_cost_model
from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.catalog import PricingCatalog


def main(argv: Optional[list[str]] = None) -> int:
    """Main CLI entry point."""
    if argv is None:
        argv = sys.argv[1:]
    
    if not argv:
        print("infra-cost-model - DAG-based infrastructure cost analysis")
        print()
        print("Commands:")
        print("  validate <yaml-file>  - Validate a cost model YAML file")
        print("  compute <yaml-file>   - Compute costs from a cost model")
        print("  analyze <yaml-file> [--json]  - Full analysis with derived usage")
        print("  extract <path>        - Extract resources from IaC (Terraform/Pulumi/CDK)")
        print("  seed-pricing          - Seed pricing cache from seed file")
        print("  graph <yaml-file>     - Render DAG visualization")
        return 0
    
    command = argv[0]
    
    if command == "validate":
        return cmd_validate(argv[1:] if len(argv) > 1 else [])
    elif command == "compute":
        return cmd_compute(argv[1:] if len(argv) > 1 else [])
    elif command == "analyze":
        return cmd_analyze(argv[1:] if len(argv) > 1 else [])
    elif command == "extract":
        return cmd_extract(argv[1:] if len(argv) > 1 else [])
    elif command == "seed-pricing":
        return cmd_seed_pricing(argv[1:] if len(argv) > 1 else [])
    elif command == "graph":
        return cmd_graph(argv[1:] if len(argv) > 1 else [])
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


def cmd_validate(args: list[str]) -> int:
    """Validate a cost model file."""
    if not args:
        print("Usage: validate <yaml-file>", file=sys.stderr)
        return 1
    
    yaml_path = Path(args[0])
    if not yaml_path.exists():
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 1
    
    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()
    
    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    errors = validate_cost_model(model)
    
    if errors:
        print("Validation errors:")
        for error in errors:
            print(f"  - {error}")
        return 1
    
    print(f"✓ Valid cost model: {yaml_path}")
    return 0


def cmd_compute(args: list[str]) -> int:
    """Compute costs from a cost model file."""
    if not args:
        print("Usage: compute <yaml-file> [--no-catalog]", file=sys.stderr)
        return 1
    
    yaml_path = Path(args[0])
    if not yaml_path.exists():
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 1
    
    use_catalog = "--no-catalog" not in args
    
    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()
    
    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    catalog = PricingCatalog() if use_catalog else None
    engine = CostEngine(model, catalog=catalog)
    
    try:
        costs = engine.compute()
        total = sum(costs.values())
        
        pricing_source = "catalog" if use_catalog else "embedded pricing rates"
        print(f"Costs for: {model['workflow']['name']} (pricing: {pricing_source})")
        print("-" * 40)
        for node, cost in sorted(costs.items()):
            print(f"  {node}: ${cost:.6f}")
        print("-" * 40)
        print(f"Total: ${total:.6f}")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_analyze(args: list[str]) -> int:
    """Full analysis including derived usage and costs."""
    if not args:
        print("Usage: analyze <yaml-file> [--json]", file=sys.stderr)
        return 1
    
    # Parse --json flag
    json_output = "--json" in args
    args = [a for a in args if a != "--json"]
    
    if not args:
        print("Usage: analyze <yaml-file> [--json]", file=sys.stderr)
        return 1
    
    yaml_path = Path(args[0])
    if not yaml_path.exists():
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 1
    
    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()
    
    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    engine = CostEngine(model, time_basis="monthly")
    
    try:
        costs = engine.compute()
        derived = engine.get_derived_usage()
        
        total = sum(costs.values())
        output = {
            "workflow": model["workflow"]["name"],
            "derived_usage": {
                addr: {"invocations_per_second": usage.invocation_count}
                for addr, usage in derived.items()
            },
            "costs": costs,
            "total_cost": total,
        }
        
        if json_output:
            print(json.dumps(output, indent=2))
        else:
            print(f"Analysis: {model['workflow']['name']}")
            print("=" * 50)
            
            print("\nDerived Usage (per second):")
            for addr, usage in sorted(derived.items()):
                print(f"  {addr}: {usage.invocation_count:.4f} invocations/sec")
            
            print("\nCosts:")
            for node, cost in sorted(costs.items()):
                print(f"  {node}: ${cost:.6f}")
            
            print("-" * 50)
            print(f"Total Monthly Cost: ${total:.6f}")
        
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_seed_pricing(args: list[str]) -> int:
    """Seed pricing catalog from seed file."""
    from infra_cost_model.pricing.sources.infracost import seed_pricing_catalog
    
    services = None
    if args and args[0] != "--all":
        services = args
    
    try:
        count, source = seed_pricing_catalog(services)
        print(f"✓ Seeded {count} prices from {source}")
        return 0
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_graph(args: list[str]) -> int:
    """Render DAG visualization."""
    if not args:
        print("Usage: graph <yaml-file>", file=sys.stderr)
        return 1
    
    yaml_path = Path(args[0])
    if not yaml_path.exists():
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 1
    
    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()
    
    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    # Check for flat override conflicts (Principle 9)
    warnings = []
    nodes = model.get("nodes", {})
    edges = model.get("edges", [])
    
    # Find all nodes that are targets of edges
    edge_targets = set()
    for edge in edges:
        edge_targets.add(edge.get("to"))
    
    # Check if any node has flatOverride AND incoming edges (conflict per DP#9)
    for node_addr, node_data in nodes.items():
        flat_override = node_data.get("flatOverride", False)
        has_incoming_edges = node_addr in edge_targets
        
        if flat_override and has_incoming_edges:
            warnings.append(
                f"⚠ Conflict: '{node_addr}' has flatOverride=true AND incoming call edges. "
                f"Flat overrides are an escape hatch (DP#9); derive usage from topology instead."
            )
    
    # Print warnings
    for warning in warnings:
        print(warning)
    
    # Render DAG
    print(f"\nDAG: {model.get('workflow', {}).get('name', 'unnamed')}")
    print("=" * 50)
    
    entry = model.get("workflow", {}).get("entry", "unknown")
    print(f"\nEntry: {entry}")
    
    # Build adjacency list
    outgoing = {}
    for edge in edges:
        src = edge.get("from")
        if src not in outgoing:
            outgoing[src] = []
        outgoing[src].append(edge)
    
    # Render nodes with their edges
    for node_addr, node_data in nodes.items():
        node_type = node_data.get("nodeType", "unknown")
        print(f"\n[{node_type.upper()}] {node_addr}")
        
        node_edges = outgoing.get(node_addr, [])
        for edge in node_edges:
            target = edge.get("to", "?")
            rate = edge.get("rate", 1.0)
            edge_type = edge.get("type", "invoke")
            arrow = "→" if edge_type == "invoke" else "→[" + edge_type[0].upper() + "]"
            print(f"  {arrow} {target} (rate: {rate})")
    
    return 0


def cmd_extract(args: list[str]) -> int:
    """Extract resources from IaC tool output.
    
    Usage: extract <path> [--from terraform|pulumi|cdk] [--json]
    
    Extracts cost model nodes from infrastructure-as-code tools.
    Supports Terraform state files, Pulumi stack exports, and CDK templates.
    """
    from pathlib import Path
    
    if not args:
        print("Usage: extract <path> [--from terraform|pulumi|cdk] [--json]", file=sys.stderr)
        print()
        print("Extract resources from infrastructure-as-code:", file=sys.stderr)
        print("  extract terraform.tfstate.json   - Extract from Terraform state", file=sys.stderr)
        print("  extract stack.json --from pulumi - Extract from Pulumi stack export", file=sys.stderr)
        print("  extract template.json --from cdk - Extract from CDK template", file=sys.stderr)
        return 1
    
    path = Path(args[0])
    source_format = "terraform"  # default
    output_json = "--json" in args
    
    # Parse --from flag
    for i, arg in enumerate(args):
        if arg == "--from" and i + 1 < len(args):
            source_format = args[i + 1]
            break
    
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    
    try:
        with open(path) as f:
            data = json.load(f)
        
        if source_format == "terraform":
            from infra_cost_model.resources.registry import extract_resources_from_tf
            nodes = extract_resources_from_tf(data)
        elif source_format == "pulumi":
            from infra_cost_model.resources.registry import extract_resources_from_pulumi
            nodes = extract_resources_from_pulumi(data)
        elif source_format == "cdk":
            from infra_cost_model.resources.registry import extract_resources_from_cdk
            nodes = extract_resources_from_cdk(data)
        else:
            print(f"Unknown source format: {source_format}", file=sys.stderr)
            print("Valid formats: terraform, pulumi, cdk", file=sys.stderr)
            return 1
        
        if output_json:
            print(json.dumps(nodes, indent=2))
        else:
            print(f"Extracted {len(nodes)} resource(s) from {source_format}:")
            for addr, node in nodes.items():
                print(f"  [{node.get('nodeType', '?')}] {addr}")
        
        return 0
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {path}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())