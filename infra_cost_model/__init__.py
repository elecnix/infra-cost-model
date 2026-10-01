"""
Infrastructure Cost Model - DAG-based cost derivation and analysis.

This package provides:
- Cost engine: DAG traversal, workload derivation, pricing, sensitivity analysis
- Resource types: Registry for cloud resource extraction
- Schema: JSON Schema validation for cost model representation
- Pricing: Multi-cloud pricing catalog with tiered/free tier support
- SDK: Python API for declaring cost models
"""

# Version of the cost engine. A model may name the version it needs
# with `requiresEngine`, and this is the value that pin is checked
# against. pyproject.toml reads it from here, so bump it in one place.
#
# Bump the minor version when a model written for this engine would
# price differently, or not at all, on the previous one.
__version__ = "0.3.0"

# Core exports
from infra_cost_model.schema import validate_cost_model
from infra_cost_model.engine import (
    CostEngine,
    DAGValidator,
    WorkloadDeriver,
    CostAggregator,
    DerivedUsage,
    SensitivityAnalyzer,
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
    "SensitivityAnalyzer",
    "Workflow",
    "Call",
    "NodeUsage",
    "Frequency",
    "per_second",
    "per_minute",
    "per_hour",
    "per_day",
]