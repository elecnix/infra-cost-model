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
from infra_cost_model.engine import CostEngine, SensitivityAnalyzer
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
        print("  whatif <yaml-file>    - What-if analysis varying a parameter")
        print("  sensitivity <yaml-file> - Sensitivity sweep across parameter range")
        print("  codegen <schema-json>  - Generate resource handler from terraform provider schema")
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
    elif command == "whatif":
        return cmd_whatif(argv[1:] if len(argv) > 1 else [])
    elif command == "sensitivity":
        return cmd_sensitivity(argv[1:] if len(argv) > 1 else [])
    elif command == "codegen":
        return cmd_codegen(argv[1:] if len(argv) > 1 else [])
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


def cmd_whatif(args: list[str]) -> int:
    """Run what-if analysis by varying a single parameter.
    
    Usage: whatif <yaml-file> --parameter <name> --value <float> [--catalog]
    
    Parameters:
        frequency             - Vary the entry frequency
        edge:from_node->to_node - Vary a specific edge call rate
    """
    if len(args) < 1:
        print("Usage: whatif <yaml-file> --parameter <name> --value <float> [--catalog]",
              file=sys.stderr)
        return 1
    
    yaml_path = Path(args[0])
    if not yaml_path.exists():
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 1
    
    # Parse flags
    parameter = None
    value = None
    use_catalog = False
    
    i = 1
    while i < len(args):
        if args[i] == "--parameter" and i + 1 < len(args):
            parameter = args[i + 1]
            i += 2
        elif args[i] == "--value" and i + 1 < len(args):
            try:
                value = float(args[i + 1])
            except ValueError:
                print(f"Invalid value: {args[i+1]}", file=sys.stderr)
                return 1
            i += 2
        elif args[i] == "--catalog":
            use_catalog = True
            i += 1
        else:
            print(f"Unknown flag: {args[i]}", file=sys.stderr)
            return 1
    
    if parameter is None:
        print("Error: --parameter is required", file=sys.stderr)
        return 1
    if value is None:
        print("Error: --value is required", file=sys.stderr)
        return 1
    
    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()
    
    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    catalog = PricingCatalog() if use_catalog else None
    
    try:
        analyzer = SensitivityAnalyzer(model, catalog)
        baseline_engine = CostEngine(model, catalog)
        baseline = baseline_engine.total_cost()
        new_cost = analyzer.what_if(parameter, value)
        delta = new_cost - baseline
        
        print(f"What-if: {model['workflow']['name']}")
        print(f"Parameter: {parameter} = {value}")
        print(f"Baseline cost: ${baseline:.6f}")
        print(f"New cost:      ${new_cost:.6f}")
        print(f"Delta:         ${delta:+.6f} ({delta/baseline*100:+.1f}%)" if baseline else f"Delta: ${delta:+.6f}")
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_sensitivity(args: list[str]) -> int:
    """Run sensitivity analysis by sweeping a parameter across a range.
    
    Usage: sensitivity <yaml-file> --parameter <name> [--steps <int>] [--catalog]
    
    Parameters:
        frequency             - Sweep entry frequency
        edge:from_node->to_node - Sweep a specific edge call rate
    
    Outputs a table of parameter values and total costs, plus JSON for
    programmatic consumption.
    """
    if len(args) < 1:
        print("Usage: sensitivity <yaml-file> --parameter <name> [--steps <int>] [--catalog]",
              file=sys.stderr)
        return 1
    
    yaml_path = Path(args[0])
    if not yaml_path.exists():
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 1
    
    # Parse flags
    parameter = None
    steps = 10
    use_catalog = False
    
    i = 1
    while i < len(args):
        if args[i] == "--parameter" and i + 1 < len(args):
            parameter = args[i + 1]
            i += 2
        elif args[i] == "--steps" and i + 1 < len(args):
            try:
                steps = int(args[i + 1])
            except ValueError:
                print(f"Invalid steps: {args[i+1]}", file=sys.stderr)
                return 1
            i += 2
        elif args[i] == "--catalog":
            use_catalog = True
            i += 1
        else:
            print(f"Unknown flag: {args[i]}", file=sys.stderr)
            return 1
    
    if parameter is None:
        print("Error: --parameter is required", file=sys.stderr)
        return 1
    
    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()
    
    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    catalog = PricingCatalog() if use_catalog else None
    
    try:
        analyzer = SensitivityAnalyzer(model, catalog)
        baseline_engine = CostEngine(model, catalog)
        baseline = baseline_engine.total_cost()
        results = analyzer.sensitivity(parameter, steps)
        
        print(f"Sensitivity: {model['workflow']['name']}")
        print(f"Parameter: {parameter}")
        print(f"Baseline: ${baseline:.6f}")
        print()
        print(f"{'Value':>12}  {'Cost':>12}  {'Delta':>12}  {'Change':>8}")
        print("-" * 52)
        
        for param_value, cost in results:
            delta_val = cost - baseline
            pct = (delta_val / baseline * 100) if baseline else 0.0
            print(f"{param_value:12.4f}  ${cost:11.6f}  ${delta_val:+11.6f}  {pct:+7.1f}%")
        
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_codegen(args: list[str]) -> int:
    """Generate resource handler from terraform provider schema.

    Usage: codegen <schema-json> [--resource <type>]

    Reads a terraform providers schema -json file and generates typed
    Python resource handler classes per DP#10.
    """
    if not args:
        print("Usage: codegen <schema-json> [--resource <type>]", file=sys.stderr)
        print()
        print("Generate resource handlers from terraform provider schema:", file=sys.stderr)
        print("  codegen providers-schema.json", file=sys.stderr)
        print("  codegen providers-schema.json --resource aws_lambda_function", file=sys.stderr)
        print()
        print("Generate the schema with:", file=sys.stderr)
        print("  terraform providers schema -json > providers-schema.json", file=sys.stderr)
        return 1

    path = Path(args[0])
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    # Parse optional --resource flag
    target_resource = None
    for i, arg in enumerate(args):
        if arg == "--resource" and i + 1 < len(args):
            target_resource = args[i + 1]
            break

    try:
        from infra_cost_model.codegen.schema_reader import SchemaReader
        from infra_cost_model.codegen.generator import CodeGenerator

        providers = SchemaReader.parse_file(str(path))
        gen = CodeGenerator()

        count = 0
        for provider in providers:
            for resource in provider.resources:
                if target_resource and resource.resource_type != target_resource:
                    continue

                source = gen.generate_handler(resource)
                print(source)
                print()  # blank line between handlers
                count += 1

        if count == 0:
            if target_resource:
                print(f"Resource type '{target_resource}' not found in schema.", file=sys.stderr)
                return 1
            else:
                print("No resources found in schema.", file=sys.stderr)
                return 1

        print(f"# Generated {count} resource handler(s) from {path}", file=sys.stderr)
        return 0

    except json.JSONDecodeError as e:
        print(f"Invalid JSON in {path}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())