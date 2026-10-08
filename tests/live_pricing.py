"""Price a node the way the engine does, for tests.

Every handler already declares which catalog row prices each of its logical
usage metrics, in ``catalog_metrics``. The engine resolves that declaration per
node through ``ResourceRegistry``. ``resource_cost`` below resolves it the same
way, so a test that calls it is asserting the production declaration rather
than a private copy of it.
"""

from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.billing_scope import query_regions
from infra_cost_model.resources.registry import ResourceRegistry

# Catalog rows are filed under a vendor, and a handler's module is where it
# lives: gcp.py holds the Google handlers, azure.py the Azure ones, and every
# other handler bills through AWS. A handler added in a new vendor module needs
# an entry here or its tests fail looking for rows that were never queried.
_VENDOR_BY_MODULE = {"gcp": "gcp", "azure": "azure", "external": "external"}


def _vendor(handler):
    """The catalog vendor a handler's rows are filed under."""
    return _VENDOR_BY_MODULE.get(handler.__module__.rsplit(".", 1)[-1], "aws")


def _query(catalog, provider, service, region, metric, quantity):
    """Query the regions the engine queries, in its order.

    ``query_regions`` is the engine's own resolver: a global metric falls
    back from the node's region to the global rows, and a regional one
    stays in its region, so a price found here is one production finds.
    """
    for candidate in query_regions(provider, service, metric, region):
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

    provider = _vendor(handler)
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

    provider = _vendor(handler)
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