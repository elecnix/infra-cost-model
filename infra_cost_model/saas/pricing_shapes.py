"""The SaaS pricing shape: a percentage of another metric's value.

A SaaS vendor's prices are data. Its rows go in
``infra_cost_model/vendors/<id>/prices.yaml``, and the catalog prices a
subscription, a per-unit rate or a free allowance from those rows (#246).

One charge does not fit a price row: a fee that is a percentage of the value
of each transaction, such as a card processor's 2.9% plus $0.30. That charge
depends on a second number, the value of one transaction, so it stays a shape.
A node declares it on the metric::

    stripe_payments:
      provider: external
      usageMetrics:
        charges: { unit: transactions, value: 1, shape: transactional,
                   percentage_rate: 0.029, volume: 50.0, fixed_per_transaction: 0.30 }

Version 0.3.0 removed the ``free_tier``, ``per_unit_flat`` and
``flat_subscription`` shapes and the ``infra_cost_model.saas_handlers``
entry-point group. A model that names a removed shape fails ``validate`` and
``compute`` with a message that says to use vendor price rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

# Shapes that a tiered price row replaced in version 0.3.0 (#246).
REMOVED_SHAPES = frozenset({"free_tier", "per_unit_flat", "flat_subscription"})


def removed_shape_message(shape: str) -> str:
    """Say what replaced a removed shape."""
    return (
        f"The '{shape}' shape was removed in version 0.3.0 (#246). Use vendor price "
        "rows instead: add the price to infra_cost_model/vendors/<id>/prices.yaml, "
        "set the node's provider to that vendor id, and remove the shape and its "
        "parameters from the metric. See CONTRIBUTING.md."
    )


class SaaSCostHandler(Protocol):
    """Protocol for a SaaS pricing-shape handler.

    A handler receives the quantity of one metric for a month and the
    metric's shape parameters from the model YAML, and returns the cost of
    that month in USD. For a fixed metric the quantity is the metric's value.
    For a usage-driven metric the engine derives a rate per second, so it
    passes the handler a month of usage and converts the monthly cost to the
    output time basis (#295).
    """

    def __call__(self, quantity: float, params: dict[str, Any]) -> float: ...


# ── Built-in shape handler ───────────────────────────────────────────────


def transactional(quantity: float, params: dict[str, Any]) -> float:
    """A percentage fee plus a fixed fee on each transaction, or a per-call fee.

    ``quantity`` is the transaction count. ``params`` may carry
    ``percentage_rate``, ``volume``, ``fixed_per_transaction`` and
    ``per_call``. ``volume`` is the value of one transaction, so each
    transaction costs ``volume × percentage_rate + fixed_per_transaction +
    per_call``. The engine's ``percentage`` pricing model uses the same
    convention (#281, #288).
    """
    percentage_rate = float(params.get("percentage_rate", 0.0))
    fixed_per_transaction = float(params.get("fixed_per_transaction", 0.0))
    per_call = float(params.get("per_call", 0.0))
    volume = float(params.get("volume", 0.0))
    return quantity * (volume * percentage_rate + fixed_per_transaction + per_call)


# ── Registry ─────────────────────────────────────────────────────────────


@dataclass
class _RegisteredHandler:
    handler: SaaSCostHandler
    name: str


class SaaSPricingRegistry:
    """Registry of named SaaS pricing-shape handlers.

    ``transactional`` is registered at module load. The schema lists the
    shapes a model may name, so ``register`` serves tests and callers that
    build a model in code, not models read from YAML.
    """

    _handlers: dict[str, _RegisteredHandler] = {}

    @classmethod
    def register(cls, name: str, handler: SaaSCostHandler) -> None:
        """Register a pricing-shape handler by name.

        Args:
            name: The shape name used in model YAML (e.g. ``"transactional"``).
            handler: A callable ``(quantity, params) -> monthly_cost_usd``.
        """
        cls._handlers[name] = _RegisteredHandler(handler=handler, name=name)

    @classmethod
    def get(cls, name: str) -> Optional[SaaSCostHandler]:
        """Look up a shape handler by name, or ``None`` if not registered."""
        entry = cls._handlers.get(name)
        return entry.handler if entry else None

    @classmethod
    def known_shapes(cls) -> set[str]:
        """Return the set of registered shape names."""
        return set(cls._handlers.keys())

    @classmethod
    def reset(cls) -> None:
        """Clear all handlers (primarily for testing)."""
        cls._handlers.clear()

    @classmethod
    def compute(cls, shape: str, quantity: float, params: dict[str, Any]) -> float:
        """Compute cost for a shaped metric.

        Raises ValueError if the shape is not registered. For a shape that
        version 0.3.0 removed, the message says to use vendor price rows.
        """
        handler = cls.get(shape)
        if handler is None:
            if shape in REMOVED_SHAPES:
                raise ValueError(removed_shape_message(shape))
            raise ValueError(f"Unknown pricing shape '{shape}'. Known shapes: {sorted(cls.known_shapes())}")
        return handler(quantity, params)


# ── Module init: register the built-in shape ─────────────────────────────

SaaSPricingRegistry.register("transactional", transactional)
