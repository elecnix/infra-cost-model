"""A run pins its price source: seed, live, or a named cache file (#446).

Without a pinned source, `compute` reads whatever the catalog finds: synced
rows when the local cache holds them, the bundled rows when it does not, and a
node's embedded `pricingRates` when neither has a row. The same model then
prices from different sources on a laptop and in CI.
"""

import sqlite3

import pytest
import yaml

from infra_cost_model.cli import main
from infra_cost_model.pricing import cache as cache_module
from infra_cost_model.pricing.cache import Price, PricingCache
from infra_cost_model.pricing.catalog import PricingCatalog

# A flat rate a sync would store for the metric below. It differs from the
# bundled seed rate, so a total tells which source answered.
LIVE_RATE = 0.0000003
SEED_PAID_RATE = 0.0000002
MONTHLY_REQUESTS = 10_000_000


def model(with_embedded_rates: bool = False) -> str:
    """A model whose single metric prices from the catalog's Lambda rows."""
    node = {
        "nodeType": "compute",
        "resourceAddress": "aws_lambda_function.fn",
        "provider": "aws",
        "service": "AWSLambda",
        "region": "us-east-1",
        "usageMetrics": {
            "Lambda-Request": {"unit": "requests", "value": 1},
        },
    }
    if with_embedded_rates:
        node["pricingRates"] = {"Lambda-Request": 0.5}
    return yaml.safe_dump({
        "version": "1.0",
        "workflow": {
            "name": "pinned",
            "entry": "fn",
            "frequency": {"unit": "perMonth", "value": MONTHLY_REQUESTS},
        },
        "nodes": {"fn": node},
        "edges": [],
    })


def lambda_model() -> str:
    """A model whose handler derives catalog quantities from logical metrics."""
    return yaml.safe_dump({
        "version": "1.0",
        "workflow": {
            "name": "derived",
            "entry": "fn",
            "frequency": {"unit": "perMonth", "value": MONTHLY_REQUESTS},
        },
        "nodes": {"fn": {
            "nodeType": "compute",
            "resourceAddress": "aws_lambda_function.fn",
            "provider": "aws",
            "service": "AWSLambda",
            "region": "us-east-1",
            "usageMetrics": {
                "invocations": {"unit": "requests", "value": 1},
                "avgDurationMs": {"unit": "ms", "value": 50},
                "memoryMb": {"unit": "MB", "value": 256},
            },
        }},
        "edges": [],
    })


@pytest.fixture
def home_cache(monkeypatch, tmp_path):
    """Point the default cache at a database this test controls."""
    db_path = tmp_path / "home" / "pricing.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setattr(cache_module, "DB_PATH", db_path)
    return db_path


@pytest.fixture
def live_db(tmp_path):
    """A cache with synced rows for the model's only metric."""
    cache = PricingCache(db_path=tmp_path / "synced" / "pricing.db")
    cache.upsert(Price(
        vendor="aws", service="AWSLambda", region="us-east-1",
        product_family="Serverless", attributes={},
        usage_metric="Lambda-Request", unit="requests", price_usd=LIVE_RATE,
        source="infracost", effective_date="2026-01-01",
        fetched_at="2026-01-01T00:00:00"))
    return cache.db_path


def write(tmp_path, text: str):
    path = tmp_path / "model.yaml"
    path.write_text(text)
    return str(path)


def total(stdout: str) -> float:
    line = [ln for ln in stdout.splitlines() if ln.startswith("Total Monthly Cost")][0]
    return float(line.split("$")[1])


def rows_by_source(db_path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {source: count for source, count in conn.execute(
            "SELECT source, COUNT(*) FROM prices GROUP BY source")}
    finally:
        conn.close()


class TestTheCatalogCanPinItsSource:
    """`PricingCatalog(sources=...)` answers from one set of rows only."""

    def test_a_live_catalog_ignores_the_bundled_rows(self, tmp_path):
        db = tmp_path / "seeded.db"
        PricingCatalog(db_path=db, seed=True)
        assert PricingCatalog(db_path=db).query(
            "aws", "AWSLambda", "us-east-1", "Lambda-Request") is not None
        assert PricingCatalog(db_path=db, sources="live").query(
            "aws", "AWSLambda", "us-east-1", "Lambda-Request") is None

    def test_a_live_catalog_answers_from_the_synced_rows(self, tmp_path, live_db):
        price = PricingCatalog(db_path=live_db, sources="live").query(
            "aws", "AWSLambda", "us-east-1", "Lambda-Request")
        assert price is not None
        assert price.price_usd == LIVE_RATE

    def test_a_seed_catalog_ignores_the_synced_rows(self, tmp_path, live_db):
        priced = PricingCatalog(db_path=live_db, sources="seed").query(
            "aws", "AWSLambda", "us-east-1", "Lambda-Request")
        assert priced.tiers[-1].price_usd == SEED_PAID_RATE

    def test_a_seed_catalog_writes_no_rows_to_the_cache_it_reads(self, live_db):
        PricingCatalog(db_path=live_db, sources="seed")
        assert rows_by_source(live_db).get("seed") is None

    def test_seed_catalogs_share_one_temporary_database(self, monkeypatch):
        # Rows load with INSERT OR IGNORE, so a second catalog can share the
        # first catalog's database. Building one per catalog would leave a
        # temporary directory behind for every `--pricing seed` run in a
        # process that prices several models (#446).
        monkeypatch.setattr(cache_module, "_BUNDLED_DB_PATH", None)
        first = PricingCatalog(sources="seed")
        second = PricingCatalog(sources="seed")
        assert first._cache.db_path == second._cache.db_path

    def test_an_empty_source_filter_matches_no_row(self, tmp_path):
        # An empty filter names no source, so it must read no row. Falling
        # through to every source would be the silent widening the filter
        # exists to prevent (#446).
        cache = PricingCache(db_path=tmp_path / "seeded.db")
        cache.upsert(Price(
            vendor="aws", service="AWSLambda", region="us-east-1",
            product_family="Serverless", attributes={},
            usage_metric="Lambda-Request", unit="requests", price_usd=LIVE_RATE,
            source="infracost", effective_date="2026-01-01",
            fetched_at="2026-01-01T00:00:00"))
        assert cache.query("aws", "AWSLambda", "us-east-1", "Lambda-Request",
                           sources=frozenset()) is None


class TestComputePricingSeed:
    """`--pricing seed` gives the same total whatever the local cache holds."""

    def test_the_total_does_not_depend_on_the_local_cache(
            self, tmp_path, live_db, capsys, monkeypatch):
        model_path = write(tmp_path, model())

        # A machine whose cache holds synced rows ...
        monkeypatch.setattr(cache_module, "DB_PATH", live_db)
        assert main(["compute", model_path, "--monthly", "--pricing", "seed"]) == 0
        with_synced = total(capsys.readouterr().out)

        # ... and one whose cache holds none give the same number.
        monkeypatch.setattr(cache_module, "DB_PATH", tmp_path / "cold" / "pricing.db")
        assert main(["compute", model_path, "--monthly", "--pricing", "seed"]) == 0
        assert total(capsys.readouterr().out) == with_synced

    def test_the_seed_total_is_the_bundled_rate_not_the_synced_one(
            self, tmp_path, home_cache, capsys, monkeypatch):
        model_path = write(tmp_path, model())

        assert main(["compute", model_path, "--monthly", "--pricing", "seed"]) == 0
        seeded = total(capsys.readouterr().out)

        # 1,000,000 requests a month are free in the bundled rows; the rest
        # pay the seed rate.
        assert seeded == pytest.approx(
            (MONTHLY_REQUESTS - 1_000_000) * SEED_PAID_RATE)

    def test_the_run_leaves_the_local_cache_without_seed_rows(
            self, tmp_path, home_cache, capsys):
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--monthly", "--pricing", "seed"]) == 0
        assert not home_cache.exists()

    def test_the_header_names_the_pinned_source(self, tmp_path, home_cache, capsys):
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--monthly", "--pricing", "seed"]) == 0
        assert "pricing: seed" in capsys.readouterr().out


class TestComputePricingLive:
    """`--pricing live` prices from synced rows and refuses anything else."""

    def test_it_fails_and_names_the_metric_without_a_synced_row(
            self, tmp_path, home_cache, capsys):
        model_path = write(tmp_path, model(with_embedded_rates=True))

        assert main(["compute", model_path, "--monthly", "--pricing", "live"]) == 1
        captured = capsys.readouterr()
        assert "--pricing live" in captured.err
        assert "Lambda-Request" in captured.err
        assert "aws" in captured.err and "AWSLambda" in captured.err
        assert "us-east-1" in captured.err
        assert "Total Monthly Cost" not in captured.out

    def test_it_fails_on_a_cold_cache(self, tmp_path, capsys):
        cold = PricingCache(db_path=tmp_path / "cold.db").db_path
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--monthly", "--pricing", "live",
                     "--pricing-db", str(cold)]) == 1
        assert "Lambda-Request" in capsys.readouterr().err

    def test_it_prices_from_the_synced_rows(self, tmp_path, live_db, capsys):
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--monthly", "--pricing", "live",
                     "--pricing-db", str(live_db)]) == 0
        assert total(capsys.readouterr().out) == pytest.approx(
            MONTHLY_REQUESTS * LIVE_RATE)

    def test_the_missing_list_names_the_row_a_sync_can_fetch(
            self, tmp_path, home_cache, capsys):
        # The model says `invocations`, `avgDurationMs` and `memoryMb`; the
        # rows it needs are the two quantities the handler derives. A list of
        # logical names would send the reader to a sync that can't fetch them.
        model_path = write(tmp_path, lambda_model())

        assert main(["compute", model_path, "--monthly", "--pricing", "live"]) == 1
        err = capsys.readouterr().err
        listed = err.split("Run sync-pricing")[0]
        assert "Lambda-Request" in listed
        assert "Lambda-GB-Second" in listed
        assert "invocations" not in listed

    def test_it_reads_the_home_cache_by_default(self, tmp_path, home_cache,
                                                live_db, capsys, monkeypatch):
        # The synced rows sit where `sync-pricing` writes them.
        monkeypatch.setattr(cache_module, "DB_PATH", live_db)
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--monthly", "--pricing", "live"]) == 0
        assert total(capsys.readouterr().out) == pytest.approx(
            MONTHLY_REQUESTS * LIVE_RATE)


class TestComputePricingDb:
    """`--pricing-db` points the run at a named cache file."""

    def test_it_reads_the_named_file(self, tmp_path, home_cache, live_db, capsys):
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--monthly",
                     "--pricing-db", str(live_db)]) == 0
        assert total(capsys.readouterr().out) == pytest.approx(
            MONTHLY_REQUESTS * LIVE_RATE)

    def test_a_run_with_no_named_file_reads_the_home_cache(
            self, tmp_path, capsys, monkeypatch):
        # The home cache holds a rate the embedded rates don't, so a total
        # that matches it can only have come from there.
        home = PricingCache(db_path=tmp_path / "home" / "pricing.db")
        home.upsert(Price(
            vendor="aws", service="AWSLambda", region="us-east-1",
            product_family="Serverless", attributes={},
            usage_metric="Lambda-Request", unit="requests", price_usd=LIVE_RATE,
            source="infracost", effective_date="2026-01-01",
            fetched_at="2026-01-01T00:00:00"))
        monkeypatch.setattr(cache_module, "DB_PATH", home.db_path)
        model_path = write(tmp_path, model())

        assert main(["compute", model_path, "--monthly"]) == 0
        assert total(capsys.readouterr().out) == pytest.approx(
            MONTHLY_REQUESTS * LIVE_RATE)

    def test_a_missing_file_is_an_error(self, tmp_path, capsys):
        model_path = write(tmp_path, model())
        absent = tmp_path / "nowhere" / "pricing.db"
        assert main(["compute", model_path, "--pricing-db", str(absent)]) == 1
        assert "no price cache" in capsys.readouterr().err


class TestPinnedSourceConflicts:
    """Options that would silently price from somewhere else are refused."""

    def test_pricing_with_no_catalog_is_an_error(self, tmp_path, capsys):
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--no-catalog",
                     "--pricing", "live"]) == 1
        assert "--no-catalog" in capsys.readouterr().err

    def test_seed_with_a_pricing_db_is_an_error(self, tmp_path, live_db, capsys):
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--pricing", "seed",
                     "--pricing-db", str(live_db)]) == 1
        assert "--pricing-db" in capsys.readouterr().err


class TestTheDefaultRunIsUnchanged:
    """Without a pinned source, the run prices as it always has."""

    def test_the_synced_rows_still_answer(self, tmp_path, home_cache, live_db,
                                          capsys, monkeypatch):
        monkeypatch.setattr(cache_module, "DB_PATH", live_db)
        model_path = write(tmp_path, model())
        assert main(["compute", model_path, "--monthly"]) == 0
        out = capsys.readouterr().out
        assert "pricing: catalog" in out
        assert total(out) == pytest.approx(MONTHLY_REQUESTS * LIVE_RATE)

    def test_embedded_rates_still_price_what_the_catalog_lacks(
            self, tmp_path, home_cache, capsys):
        model_path = write(tmp_path, model(with_embedded_rates=True))
        assert main(["compute", model_path, "--monthly"]) == 0
        assert total(capsys.readouterr().out) == pytest.approx(
            MONTHLY_REQUESTS * 0.5)
