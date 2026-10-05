"""SQLite cache layer for cloud pricing data."""

import atexit
import json
import math
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

DB_PATH = Path.home() / ".infra-cost-model" / "pricing.db"
DEFAULT_TTL_DAYS = 7

# The sources of the rows that a live sync writes. They supersede the offline
# fallback sources (#309, #376).
LIVE_SOURCES = frozenset({"infracost", "azure-retail"})

# The sources whose rows a sync fetched over a network, so their age means
# something. A staleness gate watches them (#447). The bundled offline rows --
# the seed file and the vendor files -- ship with a release rather than with a
# fetch, so they don't age out. `aws-pricelist` is in this set but not in
# LIVE_SOURCES: it reads the AWS Price List API, but it doesn't supersede the
# seed rows.
FETCHED_SOURCES = LIVE_SOURCES | {"aws-pricelist"}

# The sources of the rows that ship with the package: the seed price file and
# the vendor price files. They cost the same on every machine (#446).
BUNDLED_SOURCES = frozenset({"seed", "seed-initial", "vendor"})

# Package data, next to this module, so an installed wheel carries it (#265).
SEED_PRICES_PATH = Path(__file__).parent / "seed" / "seed_prices.json"


def _utc_now() -> datetime:
    """The current UTC time, as an aware datetime.

    Every timestamp the cache writes and every comparison against one reads
    this function, so a stored instant is the same whatever time zone the
    machine runs in (#447).
    """
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """The current UTC time as ``2026-01-15T08:00:00+00:00``."""
    return _utc_now().isoformat()


def parse_fetched_at(value: str) -> datetime | None:
    """Read a stored ``fetched_at``, or None when it isn't a timestamp.

    A value with no offset is read as UTC. Rows written before #447 carry the
    writing machine's local time and so state no offset; UTC is how every
    value written since the fix reads, which keeps one row from reporting a
    different age on two machines.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def age_since(moment: datetime, now: datetime | None = None) -> timedelta:
    """How long ago ``moment`` was, never negative.

    A timestamp in the future, from clock skew or a hand edit, reads as brand
    new rather than as a negative age. ``is_stale`` and ``status`` both
    measure a row through this, so neither can call a row stale that the
    other calls fresh, and the TTL comparison keeps its exact boundary
    (#447).
    """
    return max(timedelta(0), (now or _utc_now()) - moment)


def live_age_hours(status: dict) -> float | None:
    """The age of the newest fetched row in a ``status`` report, in hours.

    None means no fetched row has a readable timestamp, so nothing in the
    cache can be shown to be fresh.
    """
    ages = [
        entry["ageHours"]
        for source, entry in status["sources"].items()
        if source in FETCHED_SOURCES and entry["ageHours"] is not None
    ]
    return min(ages) if ages else None


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
    # When set, ``price_usd`` is the price of one block of this many ``unit``s,
    # and the quantity in this row's band is rounded up to whole blocks (#369).
    block_size: float | None = None


def billed_blocks(quantity: float, block_size: float) -> int:
    """Round ``quantity`` up to whole blocks of ``block_size``.

    A partly used block is billed whole: 1,200,000 units in blocks of
    1,000,000 is 2 blocks, and 1,000,000 is 1. A quantity of zero or less
    is no blocks. The tolerance keeps float noise, such as a quantity
    derived per second and scaled back to a month, from tipping an exact
    multiple into one more block. It is relative (a few float steps), so a
    quantity that is over a boundary by more than float noise, such as
    1,000,000.0000001 units in blocks of 1,000,000, starts the next block.
    """
    if block_size <= 0:
        raise ValueError(f"block_size must be positive, got {block_size}")
    if quantity <= 0:
        return 0
    return math.ceil(quantity / block_size - 1e-14)


def band_cost(tier: "Price", charged: float) -> float:
    """The cost of ``charged`` units that fall in ``tier``'s band."""
    if tier.block_size is not None:
        return billed_blocks(charged, tier.block_size) * tier.price_usd
    return max(0.0, charged) * tier.price_usd


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

        A row with ``block_size`` prices each started block of that many units
        at ``price_usd``: the units in its band round up to whole blocks.

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
                if tier.block_size is None:
                    return tier.price_usd * quantity
                return band_cost(tier, quantity)
            return 0.0

        for tier in sorted_tiers:
            # Scale boundaries if 'per' is specified
            multiplier = per_multiplier if tier.per else 1.0
            tier_start = (tier.start_usage_amount or 0) * multiplier
            tier_end = (tier.end_usage_amount * multiplier) if tier.end_usage_amount is not None else None

            if tier_end is None:
                if quantity > tier_start:
                    total += band_cost(tier, quantity - tier_start)
            elif quantity > tier_start:
                charged = min(quantity, tier_end) - tier_start
                total += band_cost(tier, charged)

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

    now = utc_now_iso()
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
            block_size=item.get("block_size"),
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
                    effective_date, source, fetched_at, per, block_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                price.vendor, price.service, price.region, price.product_family,
                json.dumps(price.attributes), _hash_attributes(price.attributes),
                price.usage_metric, price.unit, price.price_usd,
                price.start_usage_amount, price.end_usage_amount,
                price.purchase_option, price.effective_date, price.source,
                price.fetched_at, price.per, price.block_size,
            ))
        conn.commit()
    finally:
        conn.close()

    cache._seed_loaded = True
    return len(rows)


class PricingCache:
    """SQLite cache for cloud pricing data."""

    def __init__(self, db_path: str | Path = None, ttl_days: int = DEFAULT_TTL_DAYS,
                 seed: bool = False, sources: frozenset[str] | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.ttl_days = ttl_days
        # The sources a pinned run reads, or None for every source in
        # the cache. Every query filters on it, so a live-pinned run
        # never prices from a bundled row and the other way round (#446).
        self.sources = sources
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
        predate the ``per`` or ``block_size`` column. Fresh databases declare it inline below; for
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
                block_size REAL,
                UNIQUE(vendor, service, region, product_family, attributes_hash, usage_metric, start_usage_amount, purchase_option)
            );

            CREATE INDEX IF NOT EXISTS idx_lookup ON prices(vendor, service, region, usage_metric);
            CREATE INDEX IF NOT EXISTS idx_fetched ON prices(fetched_at);
        """)
        # Backfill `per` on any pre-existing table that lacks it. The schema above is a no-op once the
        # column exists; this line is harmless ("duplicate column name" -> caught) for fresh tables and
        # necessary for legacy files so cached Infracost rows survive an upgrade unchanged.
        for column in ("per TEXT", "block_size REAL"):
            try:
                conn.execute(f"ALTER TABLE prices ADD COLUMN {column}")
            except sqlite3.OperationalError as exc:  # duplicate column name -> already present
                if "duplicate column name" not in str(exc):
                    raise
        conn.commit()
        conn.close()

    def is_stale(self, vendor: str, service: str) -> bool:
        """Check if cached prices are older than TTL."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            # Distinct, because every row one sync wrote shares a timestamp and
            # only the instants matter, so the parse is per sync, not per row.
            "SELECT DISTINCT fetched_at FROM prices WHERE vendor = ? AND service = ?",
            (vendor, service)
        )
        stamps = [row[0] for row in cursor.fetchall()]
        conn.close()

        if not stamps:
            return True

        # MAX() in SQL would sort the timestamps as text, and a naive value
        # and one with an offset don't compare that way, so the newest is
        # chosen from the parsed instants, as status() does (#447).
        newest = max(
            (moment for moment in (parse_fetched_at(v) for v in stamps)
             if moment is not None),
            default=None,
        )
        if newest is None:
            # A row nobody can date can't be shown to be fresh (#447).
            return True
        # The same non-negative age `status` reports, so the two readers
        # can't disagree about one row (#447).
        return age_since(newest) > timedelta(days=self.ttl_days)

    def status(self) -> dict:
        """Report the rows each source wrote and how old the newest one is.

        Returns the ``path`` of the cache and a ``sources`` map from each
        source to its row count, the ``newest`` timestamp it wrote, and that
        row's ``ageHours``. A source whose rows carry no readable timestamp
        reports a null ``newest`` and ``ageHours``, so it never reads as
        fresh.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT source, fetched_at, COUNT(*) FROM prices"
                " GROUP BY source, fetched_at"
            ).fetchall()
        finally:
            conn.close()

        # Grouping in SQL would sort the timestamps as text, and a naive value
        # and one with an offset don't compare that way, so the newest of each
        # source is chosen from the parsed instants.
        counts: dict[str, int] = {}
        newest: dict[str, datetime] = {}
        for source, fetched_at, count in rows:
            counts[source] = counts.get(source, 0) + count
            moment = parse_fetched_at(fetched_at)
            if moment is None:
                continue
            if source not in newest or moment > newest[source]:
                newest[source] = moment

        now = _utc_now()
        sources: dict[str, dict] = {}
        for source in sorted(counts):
            moment = newest.get(source)
            sources[source] = {
                "rows": counts[source],
                "newest": moment.isoformat() if moment else None,
                "ageHours": (
                    round(age_since(moment, now).total_seconds() / 3600, 1)
                    if moment else None
                ),
            }
        return {"path": str(self.db_path), "sources": sources}

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
                  usage_metric: str, source: str | tuple[str, ...]):
        """Replace the rows of one metric from one source, in one transaction.

        On entry, deletes the rows with this vendor, service, region, usage
        metric and source. *source* can be a tuple of sources, when one sync
        writes rows from any of them (#376). The block then writes the new
        rows with ``upsert``. The deletion and the new rows are committed together
        when the block ends, and rolled back if it raises, so a failed sync
        keeps the old rows. Rows from other sources, such as the seed file
        and the vendor files, stay as they are (#355).
        """
        if self._replace_conn is not None:
            raise RuntimeError("PricingCache.replacing blocks can't be nested")
        sources = (source,) if isinstance(source, str) else tuple(source)
        if not sources:
            raise ValueError("PricingCache.replacing needs at least one source")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DELETE FROM prices WHERE vendor = ? AND service = ? AND region = ?"
                " AND usage_metric = ? AND source IN"
                f" ({', '.join('?' for _ in sources)})",
                (vendor, service, region, usage_metric, *sources))
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
                effective_date, source, fetched_at, per, block_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            price.vendor, price.service, price.region, price.product_family,
            json.dumps(price.attributes), attrs_hash, price.usage_metric, price.unit,
            price.price_usd, price.start_usage_amount, price.end_usage_amount,
            price.purchase_option, price.effective_date, price.source,
            price.fetched_at, price.per, price.block_size
        ))

    def query(self, vendor: str, service: str, region: str,
              usage_metric: str, quantity: float | None = None,
              sources: frozenset[str] | None = None) -> TieredPrice | Price | None:
        """Query prices for a specific vendor/service/region/usage metric.

        Returns a TieredPrice if multiple tiers exist, or a single Price.
        Returns None if no row matches.

        ``sources`` narrows the answer to rows from those sources. It defaults
        to the cache's own ``sources``, so a run that pins its price source
        never mixes one with another (#446): rows of a source the caller left
        out are as absent as rows that were never cached.
        """
        if sources is None:
            sources = self.sources
        if sources is not None and not sources:
            # An empty filter names no source, and so matches no row. Reading
            # every source instead would price from rows the caller ruled out
            # (#446).
            return None
        sql = """
            SELECT vendor, service, region, product_family, attributes,
                   usage_metric, unit, price_usd, start_usage_amount,
                   end_usage_amount, purchase_option, effective_date,
                   source, fetched_at, per, block_size
            FROM prices
            WHERE vendor = ? AND service = ? AND region = ? AND usage_metric = ?
        """
        params: tuple = (vendor, service, region, usage_metric)
        if sources is not None:
            sql += f" AND source IN ({', '.join('?' for _ in sources)})"
            params += tuple(sorted(sources))

        with closing(sqlite3.connect(self.db_path)) as conn:
            cursor = conn.execute(sql + " ORDER BY start_usage_amount", params)
            rows = cursor.fetchall()

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
                per=row[14],   # column 15 added with the `per` schema/migration
                block_size=row[15],
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
        # Rows that a live sync read from the Azure Retail Prices API count as
        # live too (#376).
        if any(p.source in LIVE_SOURCES for p in prices):
            prices = [p for p in prices if p.source in LIVE_SOURCES]

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
                p.block_size, json.dumps(p.attributes, sort_keys=True),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(p)
        prices = deduped

        if len(prices) > 1:
            return TieredPrice(tiers=prices)
        return prices[0]


def bundled_db_path() -> Path:
    """Return a private, empty database path for a bundled-rows-only catalog.

    A run that pins its price source to the bundled rows must read the same
    rows on every machine, so it takes neither the synced cache nor a path the
    caller named (#446). The directory is temporary; its removal is registered
    with `atexit`, which runs after the interpreter returns.
    """
    folder = Path(tempfile.mkdtemp(prefix="infra-cost-model-bundled-"))
    atexit.register(shutil.rmtree, folder, ignore_errors=True)
    return folder / "pricing.db"


def _hash_attributes(attrs: dict) -> str:
    """Create a stable hash of attributes dict for UNIQUE constraint."""
    return str(sorted(attrs.items()))
