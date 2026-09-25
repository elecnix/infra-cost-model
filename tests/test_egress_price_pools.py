"""Regions that share a meter or SKU share its egress tiers (#389).

Azure bills internet egress on one Bandwidth meter for each zone. The Azure
Retail Prices API gives eastus, westus2 and westeurope the same meter, with
100 GB free a month and the next tier from 10,335 GB. A model with 60 GB of
egress in eastus and 60 GB in westus2 uses 120 GB of the meter, so 20 GB are
paid, not 0.

GCP counts "the monthly usage ... by each SKU", and Cloud Run has one
internet egress SKU for each continent. us-central1 and us-east1 share the
"North America to North America" SKU and its 1 GiB free a month.
"""

import warnings

import pytest

from infra_cost_model.engine import CostEngine
from infra_cost_model.pricing.cache import Price
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.pricing.free_tiers import (
    ACCOUNT_WIDE_FREE_TIERS, SHARED_FREE_ALLOWANCES,
)
from infra_cost_model.pricing.global_services import GLOBAL_METRICS
from infra_cost_model.pricing.price_pools import PRICE_POOLS, price_pool

AZURE = ("azure", "Bandwidth", "Bandwidth-Internet-Out-GB", "GB")
CLOUD_RUN = ("gcp", "CloudRun", "CloudRun-Internet-Egress-GiB", "GiB")

# Azure zone 1: 100 GB free, $0.087 to 10,335 GB, then $0.083.
AZURE_ZONE_1 = ((0.0, 0, 100), (0.087, 100, 10335), (0.083, 10335, None))
# Azure zone 2 (Asia): the same boundaries at higher rates.
AZURE_ZONE_2 = ((0.0, 0, 100), (0.12, 100, 10335), (0.085, 10335, None))
# Cloud Run North America: 1 GiB free, $0.105 to 10 TiB.
CLOUD_RUN_NA = ((0.0, 0, 1), (0.105, 1, 10240), (0.08, 10240, None))
# Cloud Run Europe: no free GiB.
CLOUD_RUN_EU = ((0.105, 0, 10240), (0.08, 10240, None))


def _catalog(tmp_path, metric, rows_by_region):
    vendor, service, usage_metric, unit = metric
    catalog = PricingCatalog(db_path=tmp_path / "pricing.db")
    for region, tiers in rows_by_region.items():
        for price, start, end in tiers:
            catalog._cache.upsert(Price(
                vendor=vendor, service=service, region=region,
                product_family="", attributes={}, usage_metric=usage_metric,
                unit=unit, price_usd=price, start_usage_amount=start,
                end_usage_amount=end, source="test",
                fetched_at="2026-01-01T00:00:00",
            ))
    return catalog


def node(metric, region, gb):
    vendor, service, usage_metric, unit = metric
    return {
        "nodeType": "external",
        "provider": vendor,
        "service": service,
        "region": region,
        "usageMetrics": {usage_metric: {"unit": unit, "value": gb,
                                        "fixed": True}},
    }


def compute(catalog, nodes):
    names = list(nodes)
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": names[0],
                     "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": nodes,
        "edges": [{"from": a, "to": b, "type": "async", "rate": 1}
                  for a, b in zip(names, names[1:])],
    }
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return CostEngine(model, catalog=catalog,
                          time_basis="monthly").compute()


def test_azure_regions_map_to_their_meter_zone():
    key = AZURE[:3]
    assert price_pool(*key, "eastus") == price_pool(*key, "westus2")
    assert price_pool(*key, "eastus") == price_pool(*key, "westeurope")
    assert price_pool(*key, "japaneast") == price_pool(*key, "australiaeast")
    assert price_pool(*key, "brazilsouth") == price_pool(*key, "uaenorth")
    assert price_pool(*key, "eastus") != price_pool(*key, "japaneast")
    assert price_pool(*key, "eastus") != price_pool(*key, "brazilsouth")
    # A region with a meter of its own, or not in the table, has no pool.
    assert price_pool(*key, "austriaeast") is None
    # polandcentral has no meter of its own. The sync reads the zone 1
    # meter for it (#392).
    assert price_pool(*key, "polandcentral") == price_pool(*key, "eastus")
    assert price_pool(*key, "nowhere") is None


def test_gcp_regions_map_to_their_continent():
    key = CLOUD_RUN[:3]
    assert price_pool(*key, "us-central1") == price_pool(*key, "us-east1")
    assert price_pool(*key, "us-west1") == \
        price_pool(*key, "northamerica-northeast1")
    assert price_pool(*key, "europe-west1") == price_pool(*key, "europe-north1")
    assert price_pool(*key, "us-central1") != price_pool(*key, "europe-west1")
    assert price_pool(*key, "asia-east1") != \
        price_pool(*key, "australia-southeast1")
    assert price_pool(*key, "nowhere") is None


def test_metric_without_a_pool_function_has_no_pool():
    assert price_pool("aws", "AWSDataTransfer",
                      "DataTransfer-Internet-Out-GB", "us-east-1") is None


def test_pooled_metrics_are_not_in_the_other_pool_tables():
    # A metric priced here must not also be priced once for the account.
    shared = {(g.vendor, g.service, m)
              for g in SHARED_FREE_ALLOWANCES for m in g.metrics}
    for key in PRICE_POOLS:
        assert key not in GLOBAL_METRICS
        assert key not in ACCOUNT_WIDE_FREE_TIERS
        assert key not in shared


def test_two_azure_regions_in_one_zone_share_tiers(tmp_path):
    catalog = _catalog(tmp_path, AZURE, {"eastus": AZURE_ZONE_1,
                                         "westus2": AZURE_ZONE_1})
    costs = compute(catalog, {"east": node(AZURE, "eastus", 60),
                              "west": node(AZURE, "westus2", 60)})
    # 120 GB on one meter: 100 free, 20 at $0.087, split 1:1.
    assert costs["east"] + costs["west"] == pytest.approx(20 * 0.087)
    assert costs["east"] == pytest.approx(10 * 0.087)
    assert costs["west"] == pytest.approx(10 * 0.087)


def test_split_follows_each_region_quantity(tmp_path):
    catalog = _catalog(tmp_path, AZURE, {"eastus": AZURE_ZONE_1,
                                         "westeurope": AZURE_ZONE_1})
    costs = compute(catalog, {"east": node(AZURE, "eastus", 150),
                              "eu": node(AZURE, "westeurope", 50)})
    total = 100 * 0.087
    assert costs["east"] == pytest.approx(total * 0.75)
    assert costs["eu"] == pytest.approx(total * 0.25)


def test_azure_tier_boundary_counts_the_zone_total(tmp_path):
    catalog = _catalog(tmp_path, AZURE, {"eastus": AZURE_ZONE_1,
                                         "westus2": AZURE_ZONE_1})
    costs = compute(catalog, {"east": node(AZURE, "eastus", 6000),
                              "west": node(AZURE, "westus2", 6000)})
    expected = (10335 - 100) * 0.087 + (12000 - 10335) * 0.083
    assert sum(costs.values()) == pytest.approx(expected)


def test_azure_regions_in_different_zones_keep_their_own_tiers(tmp_path):
    catalog = _catalog(tmp_path, AZURE, {"eastus": AZURE_ZONE_1,
                                         "japaneast": AZURE_ZONE_2})
    costs = compute(catalog, {"east": node(AZURE, "eastus", 60),
                              "japan": node(AZURE, "japaneast", 60)})
    assert costs["east"] == pytest.approx(0.0)
    assert costs["japan"] == pytest.approx(0.0)
    costs = compute(catalog, {"east": node(AZURE, "eastus", 150),
                              "japan": node(AZURE, "japaneast", 150)})
    assert costs["east"] == pytest.approx(50 * 0.087)
    assert costs["japan"] == pytest.approx(50 * 0.12)


def test_gcp_regions_on_one_continent_share_the_sku(tmp_path):
    catalog = _catalog(tmp_path, CLOUD_RUN, {"us-central1": CLOUD_RUN_NA,
                                             "us-east1": CLOUD_RUN_NA})
    costs = compute(catalog, {"iowa": node(CLOUD_RUN, "us-central1", 1),
                              "carolina": node(CLOUD_RUN, "us-east1", 1)})
    # 2 GiB on one SKU: 1 free, 1 at $0.105.
    assert costs["iowa"] + costs["carolina"] == pytest.approx(0.105)
    assert costs["iowa"] == pytest.approx(0.0525)


def test_gcp_regions_on_different_continents_keep_their_own_skus(tmp_path):
    catalog = _catalog(tmp_path, CLOUD_RUN, {"us-central1": CLOUD_RUN_NA,
                                             "europe-west1": CLOUD_RUN_EU})
    costs = compute(catalog, {"iowa": node(CLOUD_RUN, "us-central1", 1),
                              "belgium": node(CLOUD_RUN, "europe-west1", 1)})
    assert costs["iowa"] == pytest.approx(0.0)
    assert costs["belgium"] == pytest.approx(0.105)


def test_one_region_is_unchanged(tmp_path):
    catalog = _catalog(tmp_path, AZURE, {"eastus": AZURE_ZONE_1,
                                         "westus2": AZURE_ZONE_1})
    assert compute(catalog, {"east": node(AZURE, "eastus", 150)})["east"] \
        == pytest.approx(50 * 0.087)
    costs = compute(catalog, {"a": node(AZURE, "eastus", 60),
                              "b": node(AZURE, "eastus", 60)})
    assert costs["a"] + costs["b"] == pytest.approx(20 * 0.087)


def test_pool_rows_come_from_the_first_region_in_order(tmp_path):
    # The rows of one zone are the same in every region. If a catalog holds
    # different rows, the region that comes first in alphabetical order
    # prices the pool, whatever the node order.
    other = ((0.0, 0, 100), (0.5, 100, None))
    catalog = _catalog(tmp_path, AZURE, {"eastus": AZURE_ZONE_1,
                                         "westus2": other})
    for nodes in ({"e": node(AZURE, "eastus", 60), "w": node(AZURE, "westus2", 60)},
                  {"w": node(AZURE, "westus2", 60), "e": node(AZURE, "eastus", 60)}):
        assert sum(compute(catalog, nodes).values()) == \
            pytest.approx(20 * 0.087)


def test_region_without_rows_stays_out_of_the_pool(tmp_path):
    # westus2 has no rows, so its egress is unpriced, as before.
    catalog = _catalog(tmp_path, AZURE, {"eastus": AZURE_ZONE_1})
    costs = compute(catalog, {"east": node(AZURE, "eastus", 150),
                              "west": node(AZURE, "westus2", 150)})
    assert costs["east"] == pytest.approx(50 * 0.087)


# --- Cloud Storage egress: one SKU for every region (#404) -----------------------

GCS = ("gcp", "CloudStorage", "GCS-Internet-Egress-GiB", "GiB")
# The rows a sync stores (#390, #391): 100 GiB free in us-central1, us-east1
# and us-west1 only, then $0.12 to 10 TiB, $0.11 to 150 TiB and $0.08.
GCS_FREE = ((0.0, 0, 100), (0.12, 100, 10240), (0.11, 10240, 153600),
            (0.08, 153600, None))
GCS_PAID = ((0.12, 0, 10240), (0.11, 10240, 153600), (0.08, 153600, None))


def _gcs_catalog(tmp_path):
    return _catalog(tmp_path, GCS, {"us-central1": GCS_FREE, "us-east1": GCS_FREE,
                                    "europe-west1": GCS_PAID,
                                    "asia-east1": GCS_PAID})


def test_gcs_egress_is_one_pool_for_every_region():
    key = GCS[:3]
    assert price_pool(*key, "us-central1") is not None
    assert price_pool(*key, "us-central1") == price_pool(*key, "europe-west1")
    assert price_pool(*key, "asia-east1") == price_pool(*key, "us-east1")


def test_gcs_free_egress_applies_once_to_the_free_regions(tmp_path):
    costs = compute(_gcs_catalog(tmp_path), {
        "iowa": node(GCS, "us-central1", 60), "carolina": node(GCS, "us-east1", 60)})
    # 120 GiB from the two free regions: 100 free, 20 at $0.12.
    assert costs["iowa"] == pytest.approx(10 * 0.12)
    assert costs["carolina"] == pytest.approx(10 * 0.12)


def test_gcs_free_egress_does_not_cover_other_regions(tmp_path):
    costs = compute(_gcs_catalog(tmp_path), {
        "iowa": node(GCS, "us-central1", 60), "belgium": node(GCS, "europe-west1", 60)})
    # The free 100 GiB covers the 60 GiB from us-central1 only.
    assert costs["iowa"] == pytest.approx(0.0)
    assert costs["belgium"] == pytest.approx(60 * 0.12)


def test_gcs_paid_egress_is_split_by_paid_quantity(tmp_path):
    costs = compute(_gcs_catalog(tmp_path), {
        "iowa": node(GCS, "us-central1", 150), "belgium": node(GCS, "europe-west1", 50)})
    # 100 GiB of us-central1 are free: 50 paid there and 50 in europe-west1.
    assert costs["iowa"] == pytest.approx(50 * 0.12)
    assert costs["belgium"] == pytest.approx(50 * 0.12)


def test_gcs_tier_bounds_count_every_region(tmp_path):
    costs = compute(_gcs_catalog(tmp_path), {
        "belgium": node(GCS, "europe-west1", 6000), "taiwan": node(GCS, "asia-east1", 6000)})
    assert sum(costs.values()) == pytest.approx(10240 * 0.12 + 1760 * 0.11)


def test_gcs_pool_prices_like_one_free_region(tmp_path):
    # The tier bounds count the free GiB too, as the rows of a free region
    # state. With the free 100 GiB used up, the pool costs what one free
    # region with the same total would cost, whatever the node order.
    catalog = _gcs_catalog(tmp_path)
    alone = compute(catalog, {"iowa": node(GCS, "us-central1", 12000)})["iowa"]
    for nodes in ({"iowa": node(GCS, "us-central1", 6000),
                   "belgium": node(GCS, "europe-west1", 6000)},
                  {"belgium": node(GCS, "europe-west1", 6000),
                   "iowa": node(GCS, "us-central1", 6000)}):
        assert sum(compute(catalog, nodes).values()) == pytest.approx(alone)
