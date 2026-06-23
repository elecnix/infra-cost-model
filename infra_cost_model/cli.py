"""
CLI entry point for infra-cost-model.

Provides commands for cost model validation, computation, and analysis.
Uses argparse for standardized argument parsing, --help, and error handling.
"""

import argparse
import sys
import json
from pathlib import Path
from typing import Optional

import yaml

from infra_cost_model.schema import validate_cost_model
from infra_cost_model.engine import CostEngine, SensitivityAnalyzer
from infra_cost_model.pricing.catalog import PricingCatalog


class _CLIError(Exception):
    """Raised for CLI errors to signal a specific exit code."""
    def __init__(self, code: int = 1):
        self.code = code


def _build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with subcommands and per-command help."""
    parser = argparse.ArgumentParser(
        prog="infra-cost-model",
        description="DAG-based infrastructure cost analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Override error() to raise instead of calling sys.exit
    parser.error = lambda msg: (_print_stderr(f"{parser.prog}: error: {msg}"), _raise_cli_error(1))
    # Override exit() to prevent argparse from calling sys.exit directly
    parser.exit = lambda status=0, message=None: (
        _print_stderr(message) if message else None,
        _raise_cli_error(1 if status == 2 else status),
    )

    sub = parser.add_subparsers(dest="command", title="Commands")

    # validate
    p_validate = sub.add_parser("validate", help="Validate a cost model YAML file")
    p_validate.add_argument("yaml_file", metavar="<yaml-file>", help="Path to cost model YAML file")
    p_validate.set_defaults(func=cmd_validate)

    # compute
    p_compute = sub.add_parser("compute", help="Compute costs from a cost model")
    p_compute.add_argument("yaml_file", metavar="<yaml-file>", help="Path to cost model YAML file")
    p_compute.add_argument("--no-catalog", action="store_true",
                           help="Disable pricing catalog (use embedded pricing rates)")
    p_compute.add_argument("--monthly", action="store_true",
                           help="Compute on a monthly time basis instead of per-second")
    p_compute.set_defaults(func=cmd_compute)

    # analyze
    p_analyze = sub.add_parser("analyze", help="Full analysis with derived usage")
    p_analyze.add_argument("yaml_file", metavar="<yaml-file>", help="Path to cost model YAML file")
    p_analyze.add_argument("--json", action="store_true", help="Output in JSON format")
    p_analyze.set_defaults(func=cmd_analyze)

    # extract
    p_extract = sub.add_parser("extract", help="Extract resources from IaC (Terraform/Pulumi/CDK)")
    p_extract.add_argument("path", metavar="<path>", help="Path to IaC JSON export file")
    p_extract.add_argument("--from", dest="source_format", metavar="FORMAT",
                           choices=["terraform", "pulumi", "cdk"], default="terraform",
                           help="Source format: terraform, pulumi, or cdk (default: terraform)")
    p_extract.add_argument("--json", action="store_true", help="Output in JSON format")
    p_extract.set_defaults(func=cmd_extract)

    # seed-pricing
    p_seed = sub.add_parser("seed-pricing", help="Seed pricing cache from seed file")
    p_seed.add_argument("services", nargs="*", metavar="<service>",
                        help="Specific services to seed (default: all)")
    p_seed.add_argument("--all", action="store_true", default=True,
                        help="Seed all services (default)")
    p_seed.set_defaults(func=cmd_seed_pricing)

    # graph
    p_graph = sub.add_parser("graph", help="Render DAG visualization")
    p_graph.add_argument("yaml_file", metavar="<yaml-file>", help="Path to cost model YAML file")
    p_graph.set_defaults(func=cmd_graph)

    # whatif
    p_whatif = sub.add_parser("whatif", help="What-if analysis varying a parameter")
    p_whatif.add_argument("yaml_file", metavar="<yaml-file>", help="Path to cost model YAML file")
    p_whatif.add_argument("--parameter", required=True, metavar="<name>",
                          help="Parameter to vary (e.g., frequency, edge:from->to)")
    p_whatif.add_argument("--value", required=True, type=float, metavar="<float>",
                          help="New value for the parameter")
    p_whatif.add_argument("--catalog", action="store_true",
                          help="Use pricing catalog")
    p_whatif.add_argument("--monthly", action="store_true",
                          help="Show costs in monthly terms (default: per-second)")
    p_whatif.set_defaults(func=cmd_whatif)

    # sensitivity
    p_sens = sub.add_parser("sensitivity", help="Sensitivity sweep across parameter range")
    p_sens.add_argument("yaml_file", metavar="<yaml-file>", help="Path to cost model YAML file")
    p_sens.add_argument("--parameter", required=True, metavar="<name>",
                        help="Parameter to sweep (e.g., frequency, edge:from->to)")
    p_sens.add_argument("--steps", type=int, default=10, metavar="<int>",
                        help="Number of steps (default: 10)")
    p_sens.add_argument("--catalog", action="store_true",
                        help="Use pricing catalog")
    p_sens.add_argument("--monthly", action="store_true",
                        help="Show costs in monthly terms (default: per-second)")
    p_sens.set_defaults(func=cmd_sensitivity)

    # what-if
    p_what_if = sub.add_parser("what-if", help="Parameter sweep across explicit values with optional A/B comparison")
    p_what_if.add_argument("yaml_file", metavar="<yaml-file>", help="Path to cost model YAML file")
    p_what_if.add_argument("--param", required=True, metavar="<name>",
                           help="Parameter to sweep (e.g., frequency, edge:from->to)")
    p_what_if.add_argument("--values", required=True, metavar="<v1,v2,...>",
                           help="Comma-separated parameter values to evaluate")
    p_what_if.add_argument("--output", choices=["table", "json"], default="table",
                           help="Output format: table (default) or json")
    p_what_if.add_argument("--compare", metavar="<other-model.yaml>",
                           help="Path to second cost model for A/B comparison")
    p_what_if.add_argument("--catalog", action="store_true",
                           help="Use pricing catalog")
    p_what_if.add_argument("--monthly", action="store_true",
                           help="Show costs in monthly terms (default: per-second)")
    p_what_if.set_defaults(func=cmd_what_if_sweep)

    # codegen
    p_codegen = sub.add_parser("codegen", help="Generate resource handler from terraform provider schema")
    p_codegen.add_argument("schema_json", metavar="<schema-json>", help="Path to terraform providers schema JSON file")
    p_codegen.add_argument("--resource", metavar="<type>", help="Specific resource type to generate (default: all)")
    p_codegen.set_defaults(func=cmd_codegen)

    return parser


def _print_stderr(msg: str) -> None:
    """Print to stderr."""
    print(msg, file=sys.stderr)


def _raise_cli_error(code: int = 1) -> None:
    """Raise CLI error with given exit code."""
    raise _CLIError(code)


def main(argv: Optional[list[str]] = None) -> int:
    """Main CLI entry point."""
    parser = _build_parser()

    if argv is None:
        argv = sys.argv[1:]

    # No arguments: print help
    if not argv:
        parser.print_help()
        return 0

    try:
        args = parser.parse_args(argv)

        if not hasattr(args, "func"):
            # argparse should catch this with subparsers, but handle edge case
            parser.print_help()
            return 0

        return args.func(args)

    except _CLIError as e:
        return e.code
    except SystemExit as e:
        # Normalize argparse exit code 2 to 1 for test compatibility
        code = e.code if isinstance(e.code, int) else 1
        return 1 if code == 2 else code


# --- Command implementations ---

def cmd_validate(args: argparse.Namespace) -> int:
    """Validate a cost model file."""
    yaml_path = Path(args.yaml_file)
    if not yaml_path.exists():
        _print_stderr(f"File not found: {yaml_path}")
        return 1

    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()

    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1

    errors = validate_cost_model(model)

    if errors:
        print("Validation errors:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"✓ Valid cost model: {yaml_path}")
    return 0


def cmd_compute(args: argparse.Namespace) -> int:
    """Compute costs from a cost model file."""
    yaml_path = Path(args.yaml_file)
    if not yaml_path.exists():
        _print_stderr(f"File not found: {yaml_path}")
        return 1

    use_catalog = not args.no_catalog

    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()

    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1

    catalog = PricingCatalog() if use_catalog else None
    time_basis = "monthly" if args.monthly else "perSecond"
    engine = CostEngine(model, catalog=catalog, time_basis=time_basis)

    try:
        costs = engine.compute()
        total = sum(costs.values())

        pricing_source = "catalog" if use_catalog else "embedded pricing rates"
        print(f"Costs for: {model['workflow']['name']} (pricing: {pricing_source}, {time_basis})")
        print("-" * 40)
        for node, cost in sorted(costs.items()):
            print(f"  {node}: ${cost:.6f}")
        print("-" * 40)
        label = "Total Monthly Cost" if time_basis == "monthly" else "Total"
        print(f"{label}: ${total:.6f}")
        return 0
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1


def cmd_analyze(args: argparse.Namespace) -> int:
    """Full analysis including derived usage and costs."""
    yaml_path = Path(args.yaml_file)
    if not yaml_path.exists():
        _print_stderr(f"File not found: {yaml_path}")
        return 1

    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()

    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        _print_stderr(f"Error: {e}")
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

        if args.json:
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
        _print_stderr(f"Error: {e}")
        return 1


def cmd_seed_pricing(args: argparse.Namespace) -> int:
    """Seed pricing catalog from seed file."""
    from infra_cost_model.pricing.sources.infracost import seed_pricing_catalog

    services = args.services if args.services else None

    try:
        count, source = seed_pricing_catalog(services)
        print(f"✓ Seeded {count} prices from {source}")
        return 0
    except RuntimeError as e:
        _print_stderr(f"Error: {e}")
        return 1


def cmd_graph(args: argparse.Namespace) -> int:
    """Render DAG visualization."""
    yaml_path = Path(args.yaml_file)
    if not yaml_path.exists():
        _print_stderr(f"File not found: {yaml_path}")
        return 1

    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()

    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1

    # Check for flat override conflicts (Principle 9)
    warnings_list = []
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
            warnings_list.append(
                f"⚠ Conflict: '{node_addr}' has flatOverride=true AND incoming call edges. "
                f"Flat overrides are an escape hatch (DP#9); derive usage from topology instead."
            )

    # Print warnings
    for warning in warnings_list:
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


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract resources from IaC tool output."""
    path = Path(args.path)
    if not path.exists():
        _print_stderr(f"File not found: {path}")
        return 1

    try:
        with open(path) as f:
            data = json.load(f)

        source_format = args.source_format

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
            _print_stderr(f"Unknown source format: {source_format}")
            _print_stderr("Valid formats: terraform, pulumi, cdk")
            return 1

        if args.json:
            print(json.dumps(nodes, indent=2))
        else:
            print(f"Extracted {len(nodes)} resource(s) from {source_format}:")
            for addr, node in nodes.items():
                print(f"  [{node.get('nodeType', '?')}] {addr}")

        return 0
    except json.JSONDecodeError as e:
        _print_stderr(f"Invalid JSON in {path}: {e}")
        return 1


def cmd_whatif(args: argparse.Namespace) -> int:
    """Run what-if analysis by varying a single parameter."""
    yaml_path = Path(args.yaml_file)
    if not yaml_path.exists():
        _print_stderr(f"File not found: {yaml_path}")
        return 1

    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()

    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1

    catalog = PricingCatalog() if args.catalog else None
    time_basis = "monthly" if args.monthly else "perSecond"

    try:
        analyzer = SensitivityAnalyzer(model, catalog, time_basis=time_basis)
        baseline_engine = CostEngine(model, catalog, time_basis=time_basis)
        baseline = baseline_engine.total_cost()
        new_cost = analyzer.what_if(args.parameter, args.value)
        delta = new_cost - baseline

        label = " (monthly)" if args.monthly else ""
        print(f"What-if: {model['workflow']['name']}{label}")
        print(f"Parameter: {args.parameter} = {args.value}")
        print(f"Baseline cost: ${baseline:.6f}")
        print(f"New cost:      ${new_cost:.6f}")
        if baseline:
            print(f"Delta:         ${delta:+.6f} ({delta/baseline*100:+.1f}%)")
        else:
            print(f"Delta:         ${delta:+.6f}")
        return 0
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1


def cmd_sensitivity(args: argparse.Namespace) -> int:
    """Run sensitivity analysis by sweeping a parameter across a range."""
    yaml_path = Path(args.yaml_file)
    if not yaml_path.exists():
        _print_stderr(f"File not found: {yaml_path}")
        return 1

    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()

    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1

    catalog = PricingCatalog() if args.catalog else None
    time_basis = "monthly" if args.monthly else "perSecond"

    try:
        analyzer = SensitivityAnalyzer(model, catalog, time_basis=time_basis)
        baseline_engine = CostEngine(model, catalog, time_basis=time_basis)
        baseline = baseline_engine.total_cost()
        results = analyzer.sensitivity(args.parameter, args.steps)

        label = " (monthly)" if args.monthly else ""
        print(f"Sensitivity: {model['workflow']['name']}{label}")
        print(f"Parameter: {args.parameter}")
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
        _print_stderr(f"Error: {e}")
        return 1


def cmd_what_if_sweep(args: argparse.Namespace) -> int:
    """Run what-if parameter sweep across explicit values.

    Supports single-model sweep and A/B comparison mode.
    """
    yaml_path = Path(args.yaml_file)
    if not yaml_path.exists():
        _print_stderr(f"File not found: {yaml_path}")
        return 1

    from infra_cost_model.sdk import parse_yaml_dsl
    with open(yaml_path) as f:
        content = f.read()

    try:
        model = parse_yaml_dsl(content)
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1

    # Parse values from comma-separated string
    try:
        values = [float(v.strip()) for v in args.values.split(",")]
    except ValueError:
        _print_stderr(f"Error: --values must be comma-separated numbers, got '{args.values}'")
        return 1

    if len(values) < 2:
        _print_stderr(f"Error: --values must contain at least 2 values, got {len(values)}")
        return 1

    catalog = PricingCatalog() if args.catalog else None
    time_basis = "monthly" if args.monthly else "perSecond"

    analyzer = SensitivityAnalyzer(model, catalog, time_basis=time_basis)

    # Comparison mode
    if args.compare:
        compare_path = Path(args.compare)
        if not compare_path.exists():
            _print_stderr(f"Comparison file not found: {compare_path}")
            return 1

        with open(compare_path) as f:
            compare_content = f.read()
        try:
            other_model = parse_yaml_dsl(compare_content)
        except ValueError as e:
            _print_stderr(f"Error in comparison model: {e}")
            return 1

        try:
            results = analyzer.sweep_compare(args.param, values, other_model)
        except ValueError as e:
            _print_stderr(f"Error: {e}")
            return 1

        if args.output == "json":
            import json
            print(json.dumps(results, indent=2))
            return 0

        # Table output for comparison
        _print_compare_table(model, other_model, args.param, results, time_basis)
        return 0

    # Single-model sweep
    try:
        results = analyzer.sweep_explicit(args.param, values)
    except ValueError as e:
        _print_stderr(f"Error: {e}")
        return 1

    if args.output == "json":
        import json
        print(json.dumps(results, indent=2))
        return 0

    # Table output
    _print_sweep_table(model, args.param, results, time_basis)
    return 0


def _format_param_label(value: float) -> str:
    """Format a parameter value with human-friendly suffix."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.6g}M"
    if value >= 1_000:
        return f"{value / 1_000:.6g}K"
    return f"{value:.6g}"


def _print_sweep_table(model: dict, param: str, results: list[dict],
                       time_basis: str) -> None:
    """Print a rich table for single-model what-if sweep results."""
    from rich.console import Console
    from rich.table import Table

    label_suffix = "/mo" if time_basis == "monthly" else "/sec"
    workflow_name = model.get("workflow", {}).get("name", "unnamed")

    console = Console()

    # Collect all node addresses across all results
    all_nodes: list[str] = []
    for r in results:
        for addr in r["node_costs"]:
            if addr not in all_nodes:
                all_nodes.append(addr)

    # Build table: Parameter | node1 | node2 | ... | Total
    table = Table(title=f"What-if sweep: {workflow_name} — param '{param}' ({label_suffix})")
    table.add_column(param.capitalize(), justify="right", style="cyan")
    for node in all_nodes:
        table.add_column(node, justify="right")
    table.add_column("Total", justify="right", style="bold green")

    for r in results:
        row = [_format_param_label(r["param_value"])]
        for node in all_nodes:
            cost = r["node_costs"].get(node, 0.0)
            row.append(f"${cost:.6f}")
        row.append(f"${r['total_cost']:.6f}")
        table.add_row(*row)

    console.print(table)


def _print_compare_table(model_a: dict, model_b: dict, param: str,
                         results: list[dict], time_basis: str) -> None:
    """Print a rich table for A/B comparison what-if sweep results."""
    from rich.console import Console
    from rich.table import Table

    label_suffix = "/mo" if time_basis == "monthly" else "/sec"
    name_a = model_a.get("workflow", {}).get("name", "Model A")
    name_b = model_b.get("workflow", {}).get("name", "Model B")

    console = Console()

    table = Table(title=f"What-if comparison: {name_a} vs {name_b} — param '{param}' ({label_suffix})")
    table.add_column(param.capitalize(), justify="right", style="cyan")
    table.add_column(name_a, justify="right")
    table.add_column(name_b, justify="right")
    table.add_column("Delta", justify="right", style="bold yellow")

    for r in results:
        delta_str = f"${r['delta']:+.6f}"
        table.add_row(
            _format_param_label(r["param_value"]),
            f"${r['model_a']['total_cost']:.6f}",
            f"${r['model_b']['total_cost']:.6f}",
            delta_str,
        )

    console.print(table)


def cmd_codegen(args: argparse.Namespace) -> int:
    """Generate resource handler from terraform provider schema.

    Reads a terraform providers schema -json file and generates typed
    Python resource handler classes per DP#10.
    """
    path = Path(args.schema_json)
    if not path.exists():
        _print_stderr(f"File not found: {path}")
        return 1

    target_resource = args.resource

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
                _print_stderr(f"Resource type '{target_resource}' not found in schema.")
                return 1
            else:
                _print_stderr("No resources found in schema.")
                return 1

        _print_stderr(f"# Generated {count} resource handler(s) from {path}")
        return 0

    except json.JSONDecodeError as e:
        _print_stderr(f"Invalid JSON in {path}: {e}")
        return 1
    except Exception as e:
        _print_stderr(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
