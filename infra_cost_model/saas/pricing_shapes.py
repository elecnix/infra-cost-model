"""Pluggable SaaS pricing shapes — flat subscription, per-unit, free-tier, transactional.

Closes #241: ``ExternalServiceRegistry`` only registered an address *prefix* and
the cost model for external nodes was hardcoded transactional (percentage of
volume + per-transaction + per-call). That fits Stripe/Twilio/SendGrid but not
the much larger class of SaaS vendors whose pricing is **not** a percentage of
transaction volume — flat monthly subscriptions (Datadog per-host/mo), per-unit
flat (per-organization, per-connection, per-seat), and free tiers (first N units
free, then overage).

This module promotes the external extension point from a prefix-recognizer into a
**pluggable SaaS pricing-handler registry**. Each handler computes a metric's
monthly cost from its model-declared ``shape`` and parameters, beyond the single
transactional formula. A node declares which shape each metric uses::

    workos_identity:
      provider: workos
      usageMetrics:
        WorkOS-MAU:            { unit: Users, value: 0, shape: free_tier,      free: 1000000, overage: 0.0 }
        WorkOS-SSO-Connection: { unit: Conns, value: 0, shape: per_unit_flat,   rate: 125.0 }
        WorkOS-AuditLog-Org:   { unit: Orgs,  value: 0, shape: per_unit_flat,   rate: 5.0 }
        WorkOS-CustomDomain:   { unit: Months,value: 0, shape: flat_subscription, rate: 99.0 }

so free-tier boundaries and per-unit rates live in the model, not in Python.

Third-party packages can register additional shapes via the
``infra_cost_model.saas_handlers`` entry-point group, mirroring how
``ResourceRegistry.register`` works for IaC handlers but installable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol


class SaaSCostHandler(Protocol):
    """Protocol for a SaaS pricing-shape handler.

    A handler receives the total quantity for one metric (already scaled by the
    derived invocation count for usage-driven metrics, or the raw value for
    fixed metrics) and the metric's shape parameters from the model YAML, and
    returns the monthly cost in USD.
    """

    def __call__(self, quantity: float, params: dict[str, Any]) -> float: ...


# ── Built-in shape handlers ──────────────────────────────────────────────


def flat_subscription(quantity: float, params: dict[str, Any]) -> float:
    """A fixed monthly fee, charged once regardless of quantity.

    Example: a custom domain at $99/mo. ``quantity`` is ignored — the fee is
    charged if the metric's value is > 0 (the model flips it from 0 to 1 to
    "enable" it), or charged ``quantity`` times if the caller passes a count.
    The rate comes from ``params['rate']``.

    To keep the "flip from 0 to 1" idiom ergonomic, a quantity of 0 yields 0
    (the feature is off) and any quantity >= 1 charges ``rate`` once. For
    multi-instance flat subscriptions (e.g. 3 custom domains), pass the count
    as quantity — it charges ``rate × quantity``.
    """
    rate = float(params.get("rate", 0.0))
    if quantity <= 0:
        return 0.0
    if quantity < 1:
        # A fractional quantity (shouldn't normally happen for a flat
        # subscription, but be defensive) charges once.
        return rate
    return rate * quantity


def per_unit_flat(quantity: float, params: dict[str, Any]) -> float:
    """$X × count, where count is an org/seat/connection/api-key count.

    Example: $125/mo per SSO connection, $5/org/mo for audit logs. The rate
    comes from ``params['rate']``; ``quantity`` is the unit count.
    """
    rate = float(params.get("rate", 0.0))
    return rate * quantity


def free_tier(quantity: float, params: dict[str, Any]) -> float:
    """First N units free, then $X/unit above N (optionally stepped).

    Example: WorkOS AuthKit — first 1M MAU free, then $0 overage. The free
    allowance comes from ``params['free']`` and the overage rate from
    ``params['overage']`` (default 0.0). Supports an optional ``tiers`` list
    for stepped overage::

        tiers:
          - up_to: 50000    # first 50k above the free tier at $0.01
            rate: 0.01
          - up_to: inf      # everything above 50k-overage at $0.005
            rate: 0.005
    """
    free_allowance = float(params.get("free", 0.0))
    overage = float(params.get("overage", 0.0))
    tiers = params.get("tiers")

    billable = max(0.0, quantity - free_allowance)
    if billable <= 0:
        return 0.0

    if tiers:
        # Stepped overage: walk the tier list, accumulating cost.
        cost = 0.0
        remaining = billable
        prev_cap = 0.0
        for tier in tiers:
            cap = tier.get("up_to", float("inf"))
            tier_rate = float(tier.get("rate", 0.0))
            band = cap - prev_cap
            if band <= 0:
                continue
            chunk = min(remaining, band)
            cost += chunk * tier_rate
            remaining -= chunk
            if remaining <= 0:
                break
            prev_cap = cap
        return cost

    return billable * overage


def transactional(quantity: float, params: dict[str, Any]) -> float:
    """The existing percentage/per-call shape, preserved for shape-parity.

    ``quantity`` is the transaction count. ``params`` may carry
    ``percentage_rate`` (of a separate ``volume`` param), ``fixed_per_transaction``,
    and ``per_call``. This handler exists so a transactional vendor can declare
    ``shape: transactional`` in the model rather than relying on the legacy
    ``_external_cost`` function — the shape vocabulary is exhaustive.
    """
    percentage_rate = float(params.get("percentage_rate", 0.0))
    fixed_per_transaction = float(params.get("fixed_per_transaction", 0.0))
    per_call = float(params.get("per_call", 0.0))
    volume = float(params.get("volume", 0.0))
    return volume * percentage_rate + quantity * fixed_per_transaction + quantity * per_call


# ── Registry ─────────────────────────────────────────────────────────────


@dataclass
class _RegisteredHandler:
    handler: SaaSCostHandler
    name: str


class SaaSPricingRegistry:
    """Registry of named SaaS pricing-shape handlers.

    Built-in shapes (``flat_subscription``, ``per_unit_flat``, ``free_tier``,
    ``transactional``) are registered at module load. Third-party packages
    extend the vocabulary by registering additional shapes, optionally
    discovered via the ``infra_cost_model.saas_handlers`` entry-point group.
    """

    _handlers: dict[str, _RegisteredHandler] = {}

    @classmethod
    def register(cls, name: str, handler: SaaSCostHandler) -> None:
        """Register a pricing-shape handler by name.

        Args:
            name: The shape name used in model YAML (e.g. ``"free_tier"``).
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
    def compute(cls, shape: str, quantity: float, params: dict[str, Any]) -> Optional[float]:
        """Compute cost for a shaped metric, or ``None`` if the shape is unknown.

        Returning ``None`` (rather than raising) lets the engine fall back to
        the catalog / embedded ``pricingRates`` path when a metric has no
        ``shape`` or an unregistered one — keeping the feature opt-in and
        backward-compatible.
        """
        handler = cls.get(shape)
        if handler is None:
            return None
        return handler(quantity, params)


# ── Entry-point plugin discovery ─────────────────────────────────────────


def discover_entry_point_handlers() -> None:
    """Discover and register SaaS shape handlers from the entry-point group.

    Third-party packages register a handler by adding an entry-point to the
    ``infra_cost_model.saas_handlers`` group in their ``pyproject.toml``::

        [project.entry-points."infra_cost_model.saas_handlers"]
        my_shape = "my_package.pricing:my_shape_handler"

    The entry-point value must be a callable matching the ``SaaSCostHandler``
    protocol. Each discovered callable is registered under its entry-point name.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover — Python < 3.8
        from importlib_metadata import entry_points  # type: ignore

    try:
        eps = entry_points(group="infra_cost_model.saas_handlers")
    except TypeError:
        # Python 3.9+ returns EntryPoints; older returns dict-like.
        eps = entry_points().get("infra_cost_model.saas_handlers", [])  # type: ignore

    for ep in eps:
        try:
            handler = ep.load()
            SaaSPricingRegistry.register(ep.name, handler)
        except Exception:
            # A broken plugin must not crash the engine — skip it.
            # Logging would be ideal, but this module is import-time and
            # logging may not be configured yet. Silent skip is safe.
            continue


# ── Module init: register built-ins + discover plugins ───────────────────

SaaSPricingRegistry.register("flat_subscription", flat_subscription)
SaaSPricingRegistry.register("per_unit_flat", per_unit_flat)
SaaSPricingRegistry.register("free_tier", free_tier)
SaaSPricingRegistry.register("transactional", transactional)

discover_entry_point_handlers()