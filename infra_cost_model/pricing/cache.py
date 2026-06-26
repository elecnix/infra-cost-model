"""SQLite cache layer for cloud pricing data."""

import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional
import json

DB_PATH = Path.home() / ".infra-cost-model" / "pricing.db"
DEFAULT_TTL_DAYS = 7
SEED_PRICES_PATH = Path(__file__).parent.parent.parent / "data" / "seed" / "aws_pricelist_seed.json"


@dataclass
class Price:
    """A single price record from the cache."""
    vendor: str
    service: str
    region: str
    product_family: str | None
    attributes: dict
    usage_metric: str
    unit: str
    price_usd: float
    start_usage_amount: float | None = None
    end_usage_amount: float | None = None
    purchase_option: str | None = None
    effective_date: str = ""
    source: str = ""
    fetched_at: str = ""


@dataclass
class TieredPrice:
    """Tiered pricing structure for a usage metric."""
    tiers: list[Price]
    
    def total_cost(self, quantity: float) -> float:
        """Calculate total cost for a quantity with tiered pricing."""
        sorted_tiers = sorted(
            [t for t in self.tiers if t.start_usage_amount is not None],
            key=lambda t: t.start_usage_amount or 0
        )
        
        total = 0.0
        
        # If no tiers have start_usage_amount (flat price), just multiply
        if not sorted_tiers:
            tier = self.tiers[0] if self.tiers else None
            if tier:
                return tier.price_usd * quantity
            return 0.0
        
        for tier in sorted_tiers:
            tier_start = tier.start_usage_amount or 0
            tier_end = tier.end_usage_amount
            
            if tier_end is None:
                if quantity > tier_start:
                    total += (quantity - tier_start) * tier.price_usd
            elif quantity > tier_start:
                charged = min(quantity, tier_end) - tier_start
                total += charged * tier.price_usd
        
        return total


def seed_prices(cache: Optional["PricingCache"] = None) -> int:
    """Load seed prices into cache. Returns count of prices loaded.
    
    Args:
        cache: PricingCache instance (creates default if None)
        
    Returns:
        Number of prices loaded
        
    Raises:
        RuntimeError: If seed file not found.
    """
    if cache is None:
        cache = PricingCache()
    
    if not SEED_PRICES_PATH.exists():
        raise RuntimeError(f"Seed prices file not found at {SEED_PRICES_PATH}")
    
    conn = sqlite3.connect(cache.db_path)
    count = 0
    now = datetime.now().isoformat()
    
    # Seed loading must be idempotent. SQLite treats NULL as distinct in the
    # UNIQUE constraint, so seed rows (which carry purchase_option=NULL) would
    # be re-inserted as duplicates on every load via INSERT OR IGNORE. Delete
    # ALL existing seed-sourced rows first, then insert a fresh copy from the
    # seed file. This also flips metrics that gained a $0 free-tier from flat to
    # tiered pricing without leaving stale flat rows behind (DP#13).
    conn.execute(
        "DELETE FROM prices WHERE source IN ('seed', 'seed-initial')"
    )
    conn.commit()
    
    try:
        seed_data = json.loads(SEED_PRICES_PATH.read_text())
        for item in seed_data:
            attrs_hash = _hash_attributes(item.get("attributes", {}))
            conn.execute("""
                INSERT OR IGNORE INTO prices (
                    vendor, service, region, product_family, attributes,
                    attributes_hash, usage_metric, unit, price_usd,
                    start_usage_amount, end_usage_amount, purchase_option,
                    effective_date, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get("vendor"),
                item.get("service"),
                item.get("region"),
                item.get("product_family", ""),
                json.dumps(item.get("attributes", {})),
                attrs_hash,
                item.get("usage_metric"),
                item.get("unit"),
                item.get("price_usd", 0),
                item.get("start_usage_amount"),
                item.get("end_usage_amount"),
                None,
                now,
                "seed",
                now,
            ))
            count += 1
        conn.commit()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid seed prices JSON: {e}") from e
    finally:
        conn.close()
    
    cache._seed_loaded = True
    return count


class PricingCache:
    """SQLite cache for cloud pricing data."""
    
    def __init__(self, db_path: str | Path = None, ttl_days: int = DEFAULT_TTL_DAYS):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.ttl_days = ttl_days
        self._seed_loaded = False
        self._ensure_db()
    
    def _ensure_db(self):
        """Create the database and tables if they don't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY,
                vendor TEXT NOT NULL,
                service TEXT NOT NULL,
                region TEXT NOT NULL,
                product_family TEXT,
                attributes TEXT,
                attributes_hash TEXT,
                usage_metric TEXT NOT NULL,
                unit TEXT NOT NULL,
                price_usd REAL NOT NULL,
                start_usage_amount REAL,
                end_usage_amount REAL,
                purchase_option TEXT,
                effective_date TEXT,
                source TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                UNIQUE(vendor, service, region, product_family, attributes_hash, usage_metric, start_usage_amount, purchase_option)
            );
            
            CREATE INDEX IF NOT EXISTS idx_lookup ON prices(vendor, service, region, usage_metric);
            CREATE INDEX IF NOT EXISTS idx_fetched ON prices(fetched_at);
        """)
        conn.commit()
        conn.close()
    
    def is_stale(self, vendor: str, service: str) -> bool:
        """Check if cached prices are older than TTL."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT MAX(fetched_at) FROM prices WHERE vendor = ? AND service = ?",
            (vendor, service)
        )
        result = cursor.fetchone()[0]
        conn.close()
        
        if not result:
            return True
        
        fetched = datetime.fromisoformat(result)
        return datetime.now() - fetched > timedelta(days=self.ttl_days)
    
    def upsert(self, price: Price) -> None:
        """Insert or update a price record."""
        attrs_hash = _hash_attributes(price.attributes)
        
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO prices (
                vendor, service, region, product_family, attributes,
                attributes_hash, usage_metric, unit, price_usd,
                start_usage_amount, end_usage_amount, purchase_option,
                effective_date, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            price.vendor, price.service, price.region, price.product_family,
            json.dumps(price.attributes), attrs_hash, price.usage_metric, price.unit,
            price.price_usd, price.start_usage_amount, price.end_usage_amount,
            price.purchase_option, price.effective_date, price.source,
            price.fetched_at
        ))
        conn.commit()
        conn.close()
    
    def query(self, vendor: str, service: str, region: str, 
              usage_metric: str, quantity: float | None = None) -> TieredPrice | Price | None:
        """Query prices for a specific vendor/service/region/usage metric.
        
        Returns a TieredPrice if multiple tiers exist, or a single Price.
        If no prices are found, loads seed prices and retries once.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("""
            SELECT vendor, service, region, product_family, attributes,
                   usage_metric, unit, price_usd, start_usage_amount,
                   end_usage_amount, purchase_option, effective_date,
                   source, fetched_at
            FROM prices
            WHERE vendor = ? AND service = ? AND region = ? AND usage_metric = ?
            ORDER BY start_usage_amount
        """, (vendor, service, region, usage_metric))
        
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            # Load seed prices and retry once
            if not self._seed_loaded:
                try:
                    seed_prices(self)
                    self._seed_loaded = True
                    return self.query(vendor, service, region, usage_metric, quantity)
                except RuntimeError:
                    pass
            return None
        
        prices = [
            Price(
                vendor=row[0], service=row[1], region=row[2],
                product_family=row[3], attributes=json.loads(row[4]) if row[4] else {},
                usage_metric=row[5], unit=row[6], price_usd=row[7],
                start_usage_amount=row[8], end_usage_amount=row[9],
                purchase_option=row[10], effective_date=row[11],
                source=row[12], fetched_at=row[13]
            )
            for row in rows
        ]

        # Collapse exact-duplicate rows. SQLite treats NULL as distinct in the
        # UNIQUE constraint, so rows with purchase_option=NULL (every seed row)
        # can be inserted repeatedly by re-seeding or upserts. Without this, a
        # single logical price would be returned as a spurious multi-tier
        # TieredPrice — inflating tiered costs and breaking single-Price callers.
        seen: set = set()
        deduped: list[Price] = []
        for p in prices:
            key = (
                p.product_family, p.unit, p.price_usd,
                p.start_usage_amount, p.end_usage_amount, p.purchase_option,
                json.dumps(p.attributes, sort_keys=True),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)
        prices = deduped

        if len(prices) > 1:
            return TieredPrice(tiers=prices)
        return prices[0]


def _hash_attributes(attrs: dict) -> str:
    """Create a stable hash of attributes dict for UNIQUE constraint."""
    return str(sorted(attrs.items()))
