"""
Infrastructure Cost Model - DAG-based cost derivation and analysis.

This package provides:
- Cost engine: DAG traversal, workload derivation, pricing, sensitivity analysis
- Resource types: Registry for cloud resource extraction
- Schema: JSON Schema validation for cost model representation
- Pricing: Multi-cloud pricing catalog with tiered/free tier support
- SDK: Python API for declaring cost models
"""

__version__ = "0.1.0"

# Core exports
from infra_cost_model.schema import validate_cost_model
from infra_cost_model.engine import (
    CostEngine,
    DAGValidator,
    WorkloadDeriver,
    CostAggregator,
    DerivedUsage,
)
from infra_cost_model.sdk import (
    Workflow,
    Call,
    NodeUsage,
    Frequency,
    per_second,
    per_minute,
    per_hour,
    per_day,
)

__all__ = [
    "validate_cost_model",
    "CostEngine",
    "DAGValidator",
    "WorkloadDeriver",
    "CostAggregator",
    "DerivedUsage",
    "Workflow",
    "Call",
    "NodeUsage",
    "Frequency",
    "per_second",
    "per_minute",
    "per_hour",
    "per_day",
]