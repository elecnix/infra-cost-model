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
        print("  analyze <yaml-file>   - Full analysis with derived usage")
        return 0
    
    command = argv[0]
    
    if command == "validate":
        return cmd_validate(argv[1:] if len(argv) > 1 else [])
    elif command == "compute":
        return cmd_compute(argv[1:] if len(argv) > 1 else [])
    elif command == "analyze":
        return cmd_analyze(argv[1:] if len(argv) > 1 else [])
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
    
    with open(yaml_path) as f:
        model = yaml.safe_load(f)
    
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
        print("Usage: compute <yaml-file> [--catalog]", file=sys.stderr)
        return 1
    
    yaml_path = Path(args[0])
    if not yaml_path.exists():
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 1
    
    use_catalog = "--catalog" in args
    
    with open(yaml_path) as f:
        model = yaml.safe_load(f)
    
    catalog = PricingCatalog() if use_catalog else None
    engine = CostEngine(model, catalog=catalog)
    
    try:
        costs = engine.compute()
        total = sum(costs.values())
        
        print(f"Costs for: {model['workflow']['name']}")
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
        print("Usage: analyze <yaml-file>", file=sys.stderr)
        return 1
    
    yaml_path = Path(args[0])
    if not yaml_path.exists():
        print(f"File not found: {yaml_path}", file=sys.stderr)
        return 1
    
    with open(yaml_path) as f:
        model = yaml.safe_load(f)
    
    engine = CostEngine(model)
    
    try:
        costs = engine.compute()
        derived = engine.get_derived_usage()
        
        print(f"Analysis: {model['workflow']['name']}")
        print("=" * 50)
        
        print("\nDerived Usage (per second):")
        for addr, usage in sorted(derived.items()):
            print(f"  {addr}: {usage.invocation_count:.4f} invocations")
        
        print("\nCosts:")
        total = sum(costs.values())
        for node, cost in sorted(costs.items()):
            print(f"  {node}: ${cost:.6f}")
        
        print("-" * 50)
        print(f"Total Monthly Cost: ${total:.6f}")
        
        # Also output JSON for programmatic use
        output = {
            "workflow": model["workflow"]["name"],
            "derived_usage": {
                addr: {"invocations_per_second": usage.invocation_count}
                for addr, usage in derived.items()
            },
            "costs": costs,
            "total_cost": total,
        }
        print("\n(JSON output available with --json flag)")
        
        return 0
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())