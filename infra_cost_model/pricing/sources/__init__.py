"""Pricing sources: one normalized multi-cloud source with two nested fallbacks.

``infracost`` is the source. It reads the Infracost Cloud Pricing API, which
copies AWS, GCP and Azure prices, and normalizes every vendor's rows into the
cache's own shape (Principle 13). The other two modules are below it, and
neither is an interchangeable source:

- ``aws_pricing`` is a *callee* of the Infracost adapter. ``seed_pricing_catalog``
  and ``_sync_fallback`` call ``aws_fallback_prices`` when there is no
  credential, so ``sync-pricing`` still works offline.
- ``azure_retail`` is *nested* inside the Infracost adapter. For an Azure meter
  that Infracost's own copy lacks, ``InfracostClient._fetch_prices`` reads the
  public Azure Retail Prices API and hands the rows to the same selector and
  the same store, which is why they have the fields ``query_prices`` returns.

``infracost_breakdown`` is not a price source either: it imports a breakdown
file the CLI ships with.

The fallbacks are imported from the module that owns them
(``pricing.sources.aws_pricing``, ``pricing.sources.azure_retail``), so nothing
at the package level reads as two peer sources.
"""

from .infracost import InfracostClient, sync_pricing_catalog

__all__ = ["InfracostClient", "sync_pricing_catalog"]