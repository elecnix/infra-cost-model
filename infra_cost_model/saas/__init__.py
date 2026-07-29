"""SaaS pricing-shape handlers — flat subscription, per-unit, free-tier, transactional.

See :mod:`infra_cost_model.saas.pricing_shapes` for the full module. This
package re-exports the registry and built-in handlers for convenience.
"""

from infra_cost_model.saas.pricing_shapes import (
    SaaSPricingRegistry,
    SaaSCostHandler,
    flat_subscription,
    free_tier,
    per_unit_flat,
    transactional,
    discover_entry_point_handlers,
)

__all__ = [
    "SaaSPricingRegistry",
    "SaaSCostHandler",
    "flat_subscription",
    "free_tier",
    "per_unit_flat",
    "transactional",
    "discover_entry_point_handlers",
]