"""SaaS pricing shapes: the ``transactional`` shape and its registry.

See :mod:`infra_cost_model.saas.pricing_shapes`. Other SaaS prices are vendor
price rows under ``infra_cost_model/vendors/<id>/prices.yaml``.
"""

from infra_cost_model.saas.pricing_shapes import (
    REMOVED_SHAPES,
    SaaSCostHandler,
    SaaSPricingRegistry,
    removed_shape_message,
    transactional,
)

__all__ = [
    "REMOVED_SHAPES",
    "SaaSCostHandler",
    "SaaSPricingRegistry",
    "removed_shape_message",
    "transactional",
]
