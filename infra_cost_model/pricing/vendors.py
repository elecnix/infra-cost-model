"""Vendor price loading from vendors/*/prices.yaml."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List

import yaml

from .cache import Price, PricingCache, _hash_attributes


def load_vendor_prices(cache: PricingCache) -> int:
    """Load vendor prices from vendors/*/prices.yaml into the cache.

    The function is idempotent: rows with source="vendor" are deleted first,
    then re-inserted from the YAML files. Directories whose name starts with
    '_' (e.g., _template) are skipped.

    Returns the number of price rows loaded.
    """
    repo_root = Path(__file__).parent.parent.parent
    vendors_dir = repo_root / "vendors"

    if not vendors_dir.is_dir():
        return 0

    conn = sqlite3.connect(cache.db_path)
    # Idempotent: delete existing vendor rows first
    conn.execute("DELETE FROM prices WHERE source = ?", ("vendor",))
    conn.commit()

    count = 0
    now_iso = datetime.now().isoformat()

    for prices_file in vendors_dir.glob("*/prices.yaml"):
        vendor_dir = prices_file.parent
        # Skip template directories
        if vendor_dir.name.startswith("_"):
            continue

        try:
            data = yaml.safe_load(prices_file.read_text())
        except Exception:
            continue

        if not isinstance(data, list):
            continue

        for row in data:
            try:
                price = Price(
                    vendor=str(row.get("vendor", "")).lower(),
                    service=str(row.get("service", "")),
                    region=str(row.get("region", "global")),
                    product_family=None,
                    attributes={},
                    usage_metric=str(row.get("usage_metric", "")),
                    unit=str(row.get("unit", "")),
                    price_usd=float(row.get("price_usd", 0)),
                    start_usage_amount=row.get("start_usage_amount"),
                    end_usage_amount=row.get("end_usage_amount"),
                    purchase_option=None,
                    effective_date="",
                    source="vendor",
                    fetched_at=now_iso,
                    per=row.get("per"),
                )
            except Exception:
                continue

            attrs_hash = _hash_attributes(price.attributes)
            conn.execute(
                """
                INSERT INTO prices (
                    vendor, service, region, product_family, attributes,
                    attributes_hash, usage_metric, unit, price_usd,
                    start_usage_amount, end_usage_amount, purchase_option,
                    effective_date, source, fetched_at, per
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    price.vendor,
                    price.service,
                    price.region,
                    price.product_family,
                    "{}",
                    attrs_hash,
                    price.usage_metric,
                    price.unit,
                    price.price_usd,
                    price.start_usage_amount,
                    price.end_usage_amount,
                    price.purchase_option,
                    price.effective_date,
                    price.source,
                    price.fetched_at,
                    price.per,
                ),
            )
            count += 1

    conn.commit()
    conn.close()
    return count
