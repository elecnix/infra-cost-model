"""Price a node the way the engine does, for tests.

Every handler already declares which catalog row prices each of its logical
usage metrics, in ``catalog_metrics``. The engine resolves that declaration per
node through ``ResourceRegistry``. ``resource_cost`` below resolves it the same
way, so a test that calls it is asserting the production declaration rather
than a private copy of it.
"""

from infra_cost_model.engine.engine import is_global_metric
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.global_services import GLOBAL_PRICE_REGIONS
from infra_cost_model.resources.registry import ResourceRegistry


def _query(catalog, provider, service, region, metric, quantity):
    """Query a node's region, then the global rows, as the engine does.

    A global service has one price everywhere, so a region with no rows of its
    own falls back to the global rows. Only a *global* metric may: the engine
    gates the same fallback on ``is_global_metric`` (engine.py:1300), and
    without that gate a regional metric missing from its own region would
    price here and stay unpriced in production, which is exactly the
    divergence this helper exists to rule out.
    """
    candidates = [region]
    if is_global_metric(provider, service, metric):
        candidates += [r for r in GLOBAL_PRICE_REGIONS if r != region]
    for candidate in candidates:
        result = catalog.query(provider, service, candidate, metric, quantity)
        if result is not None:
            return result
    return None


def resource_cost(address: str, service: str, region: str, *,
                  catalog=None, config=None, **usage):
    """Total monthly cost of ``usage``, keyed by logical metric name.

    Args:
        address: A resource address the registry resolves to a handler.
        service: The provider's billing service for the node, used when the
            handler does not price the metric under another service.
        region: Region whose catalog rows price the node.
        catalog: Catalog to query; a seed catalog by default.
        config: The node's config, for handlers whose mapping depends on it.
        **usage: Logical metric name to monthly quantity.
    """
    handler = ResourceRegistry.from_address(address)
    assert handler is not None, f"no handler owns {address!r}"

    provider = ResourceRegistry._infer_provider(handler)
    catalog = catalog if catalog is not None else PricingCatalog(seed=True)

    total = 0.0
    for logical, quantity in usage.items():
        if not quantity:
            continue
        metric = ResourceRegistry.resolve_catalog_metric(address, logical, config)
        assert metric is not None, (
            f"{handler.__name__} declares no catalog metric for {logical!r}"
        )
        billing_service = (
            ResourceRegistry.resolve_catalog_service(address, metric) or service
        )
        result = _query(
            catalog, provider, billing_service, region, metric, quantity
        )
        assert result is not None, (
            f"no catalog rows for {billing_service}/{metric} in {region}"
        )
        total += result.total_cost
    return total


def derived_resource_cost(address: str, service: str, region: str, usage: dict,
                          *, catalog=None, config=None):
    """Total monthly cost for a handler that derives its catalog quantities.

    Handlers such as Lambda bill quantities the model never states directly:
    duration and memory combine into GB-seconds. Those handlers expose
    ``derive_catalog_usage`` rather than ``catalog_metrics``, and this prices
    what that returns, exactly as the engine does.
    """
    handler = ResourceRegistry.from_address(address)
    assert handler is not None, f"no handler owns {address!r}"

    provider = ResourceRegistry._infer_provider(handler)
    catalog = catalog if catalog is not None else PricingCatalog(seed=True)

    derived = ResourceRegistry.derive_catalog_usage(address, usage, config)
    assert derived is not None, f"{handler.__name__} derives nothing from {usage!r}"

    total = 0.0
    for metric, quantity in derived.quantities.items():
        if not quantity:
            continue
        result = _query(catalog, provider, service, region, metric, quantity)
        assert result is not None, f"no catalog rows for {service}/{metric}"
        total += result.total_cost
    return total