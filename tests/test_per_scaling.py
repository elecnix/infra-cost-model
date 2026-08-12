"""End-to-end wiring tests for the ``per`` scaling parameter (#246).

Covers the three gaps that previously left ``Price.per`` persistently NULL even
though the data model and engine consumers can carry a symbolic multiplier. The
fixtures build an isolated SQLite database (never touching ~/.infra-cost-model)
and drive the real seed -> query path, so persistence, schema migration, and
retrieval are validated against production code rather than stubs.

Note: in this checkout the cost engine does not yet *consume* ``Price.per`` to
multiply a metric; that is upstream wiring tracked separately. These tests assert
the catalog/cache contract itself -- the foundation the engine will read. The
optional "cost scales by N" scaling assertion lives at TieredPrice on each tier,
where it is accurate and self-contained.
"""

import json as _json
import sqlite3

from infra_cost_model.pricing.cache import PricingCache, Price


def _insert_row(db_path: str, data: dict) -> None:
    """Insert one price row by explicitly-named columns (order-proof)."""
    cols = list(data.keys())
    placeholders = ",".join(["?"] * len(cols))
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        f"INSERT INTO prices ({','.join(cols)}) VALUES ({placeholders})",
        [data[c] for c in cols],
    )
    conn.commit()
    conn.close()


def test_seed_and_query_round_trip_preserves_per(tmp_path):
    """Gap 1/2/3: a price WITH per='seats' survives the seed->query cycle."""
    db = tmp_path / "pricing.db"
    PricingCache(db_path=db)

    _insert_row(str(db), {
        "vendor": "SaaS",
        "service": "Collaboration",
        "region": "global",
        "product_family": "",
        "attributes": "{}",                        # JSON blob in the attributes TEXT col
        "usage_metric": "seats_rate",
        "unit": "seats",
        "price_usd": 19.0,
        "source": "seed",
        "fetched_at": "2024-01-01T00:00:00Z",
        "per": "seats",                           # the symbolic parameter to scale by
    })

    row = PricingCache(db_path=db).query(
        vendor="SaaS", service="Collaboration", region="global", usage_metric="seats_rate")

    assert isinstance(row, Price)
    assert row.per == "seats"             # Gap 3: retrieval echoes the stored value
    assert row.price_usd == 19.0


def test_query_returns_none_per_for_unscaled_row(tmp_path):
    """A seed item lacking ``per`` comes back as ``Price.per is None``."""
    db = tmp_path / "pricing.db"
    PricingCache(db_path=db)

    _insert_row(str(db), {
        "vendor": "SaaS", "service": "Collaboration", "region": "global",
        "product_family": "", "attributes": "{}", "usage_metric": "flat_rate",
        "unit": "seats", "price_usd": 19.0, "source": "seed", "fetched_at": "2024-01-01T00:00:00Z",
    })

    row = PricingCache(db_path=db).query(
        vendor="SaaS", service="Collaboration", region="global", usage_metric="flat_rate")

    assert isinstance(row, Price)
    # No `per` column supplied here -> SQLite NULL -> Price default None (Gap 2 + Gap 3)
    assert row.per is None


def test_per_column_present_and_migrates_existing_db(tmp_path):
    """Gap 1: a fresh DB has ``per``; an old schema without it migrates in place."""
    db = tmp_path / "pricing.db"

    conn = sqlite3.connect(str(db))
    # Pre-existing table shape from before `per` existed (no per column).
    conn.executescript("""
        CREATE TABLE prices (
            id INTEGER PRIMARY KEY,
            vendor TEXT NOT NULL, service TEXT NOT NULL, region TEXT NOT NULL,
            product_family TEXT, attributes TEXT, attributes_hash TEXT,
            usage_metric TEXT NOT NULL, unit TEXT NOT NULL, price_usd REAL,
            start_usage_amount REAL, end_usage_amount REAL, purchase_option TEXT,
            effective_date TEXT, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
            UNIQUE(vendor, service, region, product_family, attributes_hash, usage_metric, start_usage_amount, purchase_option)
        );
    """)
    conn.commit()
    conn.close()

    # Constructing PricingCache against a pre-existing DB must add `per` rather than blow up.
    PricingCache(db_path=db)

    cols = [r[1] for r in sqlite3.connect(str(db))
            .execute("PRAGMA table_info(prices)").fetchall()]
    assert "per" in cols               # migration added the column


def test_catalog_query_by_usage_metric_picks_per_via_seed_path(tmp_path, monkeypatch):
    """Drive the production seed_prices -> query path with a per-bearing item."""
    import infra_cost_model.pricing.cache as cache_mod

    seed_file = tmp_path / "seed.json"
    # Only the `seats` row to keep this deterministic and hermetic. The payload's
    # keys mirror the prices schema so gap 2 reads item.get("per").
    seed_payload = [{
        "vendor": "SaaS", "service": "Collaboration", "region": "global",
        "product_family": "", "attributes": {}, "usage_metric": "seats_rate",
        "unit": "seats", "price_usd": 19.0, "start_usage_amount": None,
        "end_usage_amount": None, "purchase_option": None, "effective_date": "",
        "source": "seed", "fetched_at": "", "per": "seats",
    }]
    seed_file.write_text(_json.dumps(seed_payload))
    monkeypatch.setattr(cache_mod, "SEED_PRICES_PATH", seed_file)

    db = tmp_path / "pricing.db"
    PricingCache(db_path=db, seed=True)   # writes per='seats' via INSERT...per path

    row = PricingCache(db_path=db).query(
        vendor="SaaS", service="Collaboration", region="global", usage_metric="seats_rate")

    assert isinstance(row, Price)
    assert row.per == "seats"


def test_seed_without_per_yields_none_on_query(tmp_path, monkeypatch):
    """seed_prices honours an absent ``per`` key: item.get('per') -> NULL -> None."""
    import infra_cost_model.pricing.cache as cache_mod

    seed_file = tmp_path / "seed.json"
    raw = [{
        "vendor": "SaaS", "service": "Collaboration", "region": "global",
        "product_family": "", "attributes": {}, "usage_metric": "flat_rate",
        "unit": "seats", "price_usd": 19.0, "start_usage_amount": None,
        "end_usage_amount": None, "purchase_option": None, "effective_date": "",
        "source": "seed", "fetched_at": "",                          # no `per` key at all
    }]
    seed_file.write_text(_json.dumps(raw))
    monkeypatch.setattr(cache_mod, "SEED_PRICES_PATH", seed_file)

    db = tmp_path / "pricing.db"
    PricingCache(db_path=db, seed=True)

    row = PricingCache(db_path=db).query(
        vendor="SaaS", service="Collaboration", region="global", usage_metric="flat_rate")

    assert isinstance(row, Price)
    assert row.per is None   # gap 2 default item.get('per') -> NULL -> Price(None)