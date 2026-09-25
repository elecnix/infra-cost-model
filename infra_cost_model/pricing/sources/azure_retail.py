"""Azure Retail Prices API client (#372, #376).

The Infracost Cloud Pricing API copies its Azure prices from this public API,
which needs no credential. The Infracost copy lacks some meters in some
regions, such as the Blob Storage "Hot LRS Write Operations" meter in
eastus2 (#376), and gives the Bandwidth meter with the tiers of an older
price list beside the current ones (#372). A live sync reads such a meter
from this API.

The prices come back in the same form as ``InfracostClient.query_prices``,
so the Infracost descriptors select and store them the same way.
"""

import os

import requests

AZURE_RETAIL_PRICES_URL = os.getenv(
    "AZURE_RETAIL_PRICES_ENDPOINT", "https://prices.azure.com/api/retail/prices"
)

# The cache source of the rows that this API gives.
SOURCE = "azure-retail"

# The item fields that the Infracost API keeps as a product's attributes.
_ATTRIBUTES = ("productName", "skuName", "meterName", "meterId", "productId",
               "serviceId", "armSkuName", "serviceFamily", "effectiveStartDate")


def _quoted(value: str) -> str:
    # OData writes a quote in a string as two quotes.
    return "'" + value.replace("'", "''") + "'"


def query_azure_retail_prices(service: str, region: str,
                              attribute_filters: list[dict] | None) -> list[dict]:
    """Return the consumption prices of *service* in *region*.

    *attribute_filters* are the descriptor's Infracost filters, such as
    ``{"key": "meterName", "value": "Hot LRS Write Operations"}``. Each one
    must equal the item field of the same name. When a meter has prices
    with more than one effective date, only the latest ones are kept. Items
    without a price or a meter ID are skipped.
    """
    clauses = [f"serviceName eq {_quoted(service)}",
               f"armRegionName eq {_quoted(region)}",
               "priceType eq 'Consumption'"]
    clauses += [f"{f['key']} eq {_quoted(f['value'])}" for f in attribute_filters or []]
    response = requests.get(AZURE_RETAIL_PRICES_URL,
                            params={"$filter": " and ".join(clauses)}, timeout=30)
    items = []
    while True:
        response.raise_for_status()
        page = response.json()
        items += page.get("Items") or []
        link = page.get("NextPageLink")
        if not link:
            break
        response = requests.get(link, timeout=30)

    # An item without a price or a meter can't give a row.
    items = [item for item in items
             if item.get("retailPrice") is not None and item.get("meterId")]
    latest: dict[str, str] = {}
    for item in items:
        meter = item.get("meterId")
        latest[meter] = max(latest.get(meter, ""), item.get("effectiveStartDate") or "")
    return [
        {
            "vendor": "azure", "service": service, "region": region,
            "product_family": item.get("serviceFamily"),
            "attributes": {k: item[k] for k in _ATTRIBUTES if k in item},
            "unit": item.get("unitOfMeasure"),
            "price_usd": float(item["retailPrice"]),
            "start_usage_amount": float(item.get("tierMinimumUnits") or 0),
            "end_usage_amount": None,
            "source": SOURCE,
        }
        for item in items
        if (item.get("effectiveStartDate") or "") == latest[item.get("meterId")]
    ]
