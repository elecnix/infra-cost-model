"""Vendor price loading from the bundled ``vendors`` package."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from importlib import resources
from typing import Any

import yaml

from .cache import Price, PricingCache, _hash_attributes

_REQUIRED_STRINGS = ("vendor", "service", "usage_metric", "unit")
_OPTIONAL_NUMBERS = ("start_usage_amount", "end_usage_amount")


def _row_error(source: str, index: int, message: str) -> ValueError:
    return ValueError(f"{source}, row {index}: {message}")


def _parse_row(row: Any, source: str, index: int, fetched_at: str) -> Price:
    if not isinstance(row, dict):
        raise _row_error(source, index, "expected a mapping")

    for field in _REQUIRED_STRINGS:
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise _row_error(source, index, f"'{field}' must be a non-empty string")

    region = row.get("region", "global")
    if not isinstance(region, str) or not region.strip():
        raise _row_error(source, index, "'region' must be a non-empty string")

    raw_price = row.get("price_usd")
    if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float)):
        raise _row_error(source, index, "'price_usd' must be a number")
    price_usd = float(raw_price)
    if not math.isfinite(price_usd):
        raise _row_error(source, index, "'price_usd' must be finite")

    optional_numbers: dict[str, float | None] = {}
    for field in _OPTIONAL_NUMBERS:
        value = row.get(field)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise _row_error(source, index, f"'{field}' must be a number or null")
        number = float(value) if value is not None else None
        if number is not None and not math.isfinite(number):
            raise _row_error(source, index, f"'{field}' must be finite")
        optional_numbers[field] = number
    if (
        optional_numbers["start_usage_amount"] is not None
        and optional_numbers["end_usage_amount"] is not None
        and optional_numbers["end_usage_amount"] <= optional_numbers["start_usage_amount"]
    ):
        raise _row_error(source, index, "'end_usage_amount' must exceed 'start_usage_amount'")

    per = row.get("per")
    if per is not None and (not isinstance(per, str) or not per.strip()):
        raise _row_error(source, index, "'per' must be a non-empty string or null")

    return Price(
        vendor=row["vendor"].lower(),
        service=row["service"],
        region=region,
        product_family=None,
        attributes={},
        usage_metric=row["usage_metric"],
        unit=row["unit"],
        price_usd=price_usd,
        start_usage_amount=optional_numbers["start_usage_amount"],
        end_usage_amount=optional_numbers["end_usage_amount"],
        purchase_option=None,
        effective_date="",
        source="vendor",
        fetched_at=fetched_at,
        per=per,
    )


def _read_vendor_prices() -> list[Price]:
    try:
        vendors_root = resources.files("vendors")
    except (ModuleNotFoundError, TypeError):
        return []

    fetched_at = datetime.now().isoformat()
    parsed: list[Price] = []
    for vendor_dir in sorted(vendors_root.iterdir(), key=lambda item: item.name):
        if not vendor_dir.is_dir() or vendor_dir.name.startswith("_"):
            continue
        prices_file = vendor_dir.joinpath("prices.yaml")
        if not prices_file.is_file():
            continue
        source = f"vendors/{vendor_dir.name}/prices.yaml"
        try:
            data = yaml.safe_load(prices_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(f"{source}: invalid YAML: {exc}") from exc
        if not isinstance(data, list):
            raise ValueError(f"{source}: expected a list of price rows")
        parsed.extend(
            _parse_row(row, source, index, fetched_at)
            for index, row in enumerate(data, start=1)
        )
    return parsed


def load_vendor_prices(cache: PricingCache) -> int:
    """Atomically replace cached vendor prices with validated bundled data."""
    prices = _read_vendor_prices()
    if not prices:
        return 0

    conn = sqlite3.connect(cache.db_path)
    try:
        with conn:
            conn.execute("DELETE FROM prices WHERE source = ?", ("vendor",))
            for price in prices:
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
                        json.dumps(price.attributes),
                        _hash_attributes(price.attributes),
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
    finally:
        conn.close()
    return len(prices)
