"""SQLite cache layer for cloud pricing data."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional
import json

DB_PATH = Path.home() / ".infra-cost-model" / "pricing.db"
DEFAULT_TTL_DAYS = 7
# Package data, next to this module, so an installed wheel carries it (#265).
SEED_PRICES_PATH = Path(__file__).parent / "seed" / "seed_prices.json"


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
    per: str | None = None


@dataclass
class TieredPrice:
    """Tiered pricing structure for a usage metric."""
    tiers: list[Price]

    def total_cost(self, quantity: float, per_multiplier: float = 1.0) -> float:
        """Calculate total cost for a quantity with tiered pricing.

        ``quantity`` is in the row's own unit and stays raw. ``per_multiplier``
        is the resolved value of the row's ``per`` parameter, which converts a
        boundary stated per unit into that raw unit: a row whose included band
        is 1,900 credits per seat has 47,500 included at 25 seats.

        The multiplier scales boundaries only. It does not scale ``price_usd``:
        a seat row priced at $19 with ``per: seats`` charges $19 per seat, so
        25 seats costs $475.

        A row with no boundary has nothing for the multiplier to move, and its
        price is charged once per unit of ``quantity``.
        """
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
            # Scale boundaries if 'per' is specified
            multiplier = per_multiplier if tier.per else 1.0
            tier_start = (tier.start_usage_amount or 0) * multiplier
            tier_end = (tier.end_usage_amount * multiplier) if tier.end_usage_amount is not None else None
            price = tier.price_usd

            if tier_end is None:
                if quantity > tier_start:
                    total += (quantity - tier_start) * price
            elif quantity > tier_start:
                charged = min(quantity, tier_end) - tier_start
                total += charged * price

        return total


class SeedFileNotFound(RuntimeError):
    """The seed price file is not at SEED_PRICES_PATH."""


def load_seed_rows(services: list[str] | None = None) -> list[Price]:
    """Read the seed file and return its rows as prices.

    Every caller that reads the seed file goes through this function, so they
    all read the same path and keep the same fields. It reads
    ``SEED_PRICES_PATH`` when called, so a test that patches it changes what
    every caller reads.

    Args:
        services: Service names to keep. None keeps every row.

    Returns:
        One Price per row, with the row's ``attributes`` and ``per`` and the
        source ``"seed"``.

    Raises:
        SeedFileNotFound: If the seed file doesn't exist.
        RuntimeError: If the seed file isn't valid JSON.
    """
    path = SEED_PRICES_PATH
    if not path.exists():
        raise SeedFileNotFound(f"Seed prices file not found at {path}")
    try:
        seed_data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid seed prices JSON: {e}") from e

    now = datetime.now().isoformat()
    return [
        Price(
            vendor=item.get("vendor"),
            service=item.get("service"),
            region=item.get("region"),
            product_family=item.get("product_family", ""),
            attributes=item.get("attributes", {}),
            usage_metric=item.get("usage_metric"),
            unit=item.get("unit"),
            price_usd=item.get("price_usd", 0),
            start_usage_amount=item.get("start_usage_amount"),
            end_usage_amount=item.get("end_usage_amount"),
            purchase_option=None,
            effective_date=now,
            source="seed",
            fetched_at=now,
            per=item.get("per"),
        )
        for item in seed_data
        if services is None or item.get("service") in services
    ]


def seed_prices(cache: Optional["PricingCache"] = None) -> int:
    """Load every seed price into the cache. Returns count of prices loaded.

    Args:
        cache: PricingCache instance (creates default if None)

    Returns:
        Number of prices loaded

    Raises:
        SeedFileNotFound: If the seed file doesn't exist.
        RuntimeError: If the seed file isn't valid JSON.
    """
    if cache is None:
        cache = PricingCache()

    rows = load_seed_rows()

    conn = sqlite3.connect(cache.db_path)
    try:
        # Seed loading must be idempotent. SQLite treats NULL as distinct in
        # the UNIQUE constraint, so seed rows (which carry
        # purchase_option=NULL) would be re-inserted as duplicates on every
        # load via INSERT OR IGNORE. Delete ALL existing seed-sourced rows
        # first, then insert a fresh copy from the seed file. This also flips
        # metrics that gained a $0 free-tier from flat to tiered pricing
        # without leaving stale flat rows behind (DP#13). "seed-initial" is
        # the label older versions gave rows loaded for a list of services.
        conn.execute(
            "DELETE FROM prices WHERE source IN ('seed', 'seed-initial')"
        )
        for price in rows:
            conn.execute("""
                INSERT OR IGNORE INTO prices (
                    vendor, service, region, product_family, attributes,
                    attributes_hash, usage_metric, unit, price_usd,
                    start_usage_amount, end_usage_amount, purchase_option,
                    effective_date, source, fetched_at, per
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                price.vendor, price.service, price.region, price.product_family,
                json.dumps(price.attributes), _hash_attributes(price.attributes),
                price.usage_metric, price.unit, price.price_usd,
                price.start_usage_amount, price.end_usage_amount,
                price.purchase_option, price.effective_date, price.source,
                price.fetched_at, price.per,
            ))
        conn.commit()
    finally:
        conn.close()

    cache._seed_loaded = True
    return len(rows)


class PricingCache:
    """SQLite cache for cloud pricing data."""

    def __init__(self, db_path: str | Path = None, ttl_days: int = DEFAULT_TTL_DAYS,
                 seed: bool = False):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.ttl_days = ttl_days
        self._seed_loaded = False
        # The connection of an open `replacing` block, which `upsert` writes to.
        self._replace_conn: sqlite3.Connection | None = None
        self._ensure_db()
        if seed:
            seed_prices(self)
        # Load vendor prices unconditionally
        from .vendors import load_vendor_prices
        load_vendor_prices(self)

    def _ensure_db(self):
        """Create the database and tables (migrating in any missing columns).

        Existing on-disk databases -- e.g. ``~/.infra-cost-model/pricing.db`` -- may
        predate the ``per`` column. Fresh databases declare it inline below; for
        legacy files we backfill with a guarded ``ALTER TABLE`` so cached pricing
        rows are never dropped across upgrades.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS prices (
                id INTEGER PRIMARY KEY,
                vendor TEXT NOT NULL, service TEXT NOT NULL, region TEXT NOT NULL,
                product_family TEXT, attributes TEXT, attributes_hash TEXT,
                usage_metric TEXT NOT NULL, unit TEXT NOT NULL, price_usd REAL NOT NULL,
                start_usage_amount REAL, end_usage_amount REAL, purchase_option TEXT,
                effective_date TEXT, source TEXT NOT NULL, fetched_at TEXT NOT NULL, per TEXT,
                UNIQUE(vendor, service, region, product_family, attributes_hash, usage_metric, start_usage_amount, purchase_option)
            );

            CREATE INDEX IF NOT EXISTS idx_lookup ON prices(vendor, service, region, usage_metric);
            CREATE INDEX IF NOT EXISTS idx_fetched ON prices(fetched_at);
        """)
        # Backfill `per` on any pre-existing table that lacks it. The schema above is a no-op once the
        # column exists; this line is harmless ("duplicate column name" -> caught) for fresh tables and
        # necessary for legacy files so cached Infracost rows survive an upgrade unchanged.
        try:
            conn.execute("ALTER TABLE prices ADD COLUMN per TEXT")
        except sqlite3.OperationalError as exc:  # duplicate column name -> already present
            if "duplicate column name" not in str(exc):
                raise
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

    def source_info(self) -> dict[str, int]:
        """Return a count of rows by pricing source.

        Keys may include 'infracost', 'seed', 'aws-pricelist', etc.
        An empty dict means no rows at all.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT source, COUNT(*) FROM prices GROUP BY source"
        )
        result = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()
        return result

    @contextmanager
    def replacing(self, vendor: str, service: str, region: str,
                  usage_metric: str, source: str):
        """Replace the rows of one metric from one source, in one transaction.

        On entry, deletes the rows with this vendor, service, region, usage
        metric and source. The block then writes the new rows with
        ``upsert``. The deletion and the new rows are committed together
        when the block ends, and rolled back if it raises, so a failed sync
        keeps the old rows. Rows from other sources, such as the seed file
        and the vendor files, stay as they are (#355).
        """
        if self._replace_conn is not None:
            raise RuntimeError("PricingCache.replacing blocks can't be nested")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DELETE FROM prices WHERE vendor = ? AND service = ? AND region = ?"
                " AND usage_metric = ? AND source = ?",
                (vendor, service, region, usage_metric, source))
            self._replace_conn = conn
            yield
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._replace_conn = None
            conn.close()

    def upsert(self, price: Price) -> None:
        """Insert or update a price record.

        Inside a ``replacing`` block, the row is written in that block's
        transaction.
        """
        attrs_hash = _hash_attributes(price.attributes)

        if self._replace_conn is not None:
            self._write(self._replace_conn, price, attrs_hash)
            return
        conn = sqlite3.connect(self.db_path)
        try:
            self._write(conn, price, attrs_hash)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _write(conn: sqlite3.Connection, price: Price, attrs_hash: str) -> None:
        conn.execute("""
            INSERT OR REPLACE INTO prices (
                vendor, service, region, product_family, attributes,
                attributes_hash, usage_metric, unit, price_usd,
                start_usage_amount, end_usage_amount, purchase_option,
                effective_date, source, fetched_at, per
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            price.vendor, price.service, price.region, price.product_family,
            json.dumps(price.attributes), attrs_hash, price.usage_metric, price.unit,
            price.price_usd, price.start_usage_amount, price.end_usage_amount,
            price.purchase_option, price.effective_date, price.source,
            price.fetched_at, price.per
        ))

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
                   source, fetched_at, per
            FROM prices
            WHERE vendor = ? AND service = ? AND region = ? AND usage_metric = ?
            ORDER BY start_usage_amount
        """, (vendor, service, region, usage_metric))

        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return None

        prices = [
            Price(
                vendor=row[0], service=row[1], region=row[2],
                product_family=row[3], attributes=json.loads(row[4]) if row[4] else {},
                usage_metric=row[5], unit=row[6], price_usd=row[7],
                start_usage_amount=row[8], end_usage_amount=row[9],
                purchase_option=row[10], effective_date=row[11],
                source=row[12], fetched_at=row[13],
                per=row[14]   # column 15 added with the `per` schema/migration
            )
            for row in rows
        ]

        # Live prices supersede every offline fallback source: seed,
        # aws-pricelist, and seed-initial. Older versions wrote seed-initial
        # rows; a cache built by one keeps them until the next seed load
        # deletes them (#309). Those fixtures are not a second pricing schedule: mixing
        # a fallback row (start_usage_amount=None) with a live row
        # (start_usage_amount=0.0) for the same metric yields a spurious 2-tier
        # TieredPrice whose open-ended tiers each charge the full quantity --
        # double-counting the cost. When any live (Infracost) row is present for
        # this key, keep only the live rows so the two schedules never mix. This
        # preserves a genuine multi-tier live schedule (all rows are 'infracost').
        if any(p.source == "infracost" for p in prices):
            prices = [p for p in prices if p.source == "infracost"]

        # Collapse exact-duplicate rows. SQLite treats NULL as distinct in the
        # UNIQUE constraint, so rows with purchase_option=NULL (every seed row)
        # can be inserted repeatedly by re-seeding or upserts. Without this, a
        # single logical price would be returned as a spurious multi-tier
        # TieredPrice -- inflating tiered costs and breaking single-Price callers.
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