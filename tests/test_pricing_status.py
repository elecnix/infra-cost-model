"""Tests for the UTC cache timestamps and the `pricing-status` command (#447).

A cache written on a CI runner in UTC and read on a workstation in another
time zone must report the same row age, so every writer stamps an explicit
UTC offset and every reader treats a naive value as UTC. The status command
reports that age per source, so no caller parses `fetched_at` itself.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from infra_cost_model.cli import main
from infra_cost_model.pricing import cache as cache_module
from infra_cost_model.pricing.cache import (
    Price,
    PricingCache,
    live_age_hours,
    load_seed_rows,
    parse_fetched_at,
    utc_now_iso,
)

# A fixed instant the tests compare against, so no assertion depends on the
# clock ticking during the run.
NOW = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_now(monkeypatch):
    """Pin the clock the cache compares stored timestamps against."""
    monkeypatch.setattr(cache_module, "_utc_now", lambda: NOW)
    return NOW


def _price(source: str, fetched_at: str, metric: str = "M1",
           vendor: str = "test") -> Price:
    return Price(
        vendor=vendor, service="Probe", region="us-west-2", product_family="",
        attributes={}, usage_metric=metric, unit="U", price_usd=1.0,
        start_usage_amount=None, end_usage_amount=None, purchase_option=None,
        source=source, effective_date="2026-01-01", fetched_at=fetched_at,
    )


class TestUtcTimestamps:
    """Every timestamp the cache writes states its offset."""

    def test_utc_now_iso_states_the_offset(self):
        stamped = utc_now_iso()
        assert stamped.endswith("+00:00")
        moment = parse_fetched_at(stamped)
        assert moment.utcoffset() == timedelta(0)
        assert abs(datetime.now(timezone.utc) - moment) < timedelta(minutes=1)

    def test_seed_rows_carry_an_aware_utc_timestamp(self):
        rows = load_seed_rows(["AWSLambda"])
        assert rows
        for row in rows:
            moment = parse_fetched_at(row.fetched_at)
            assert moment is not None
            assert moment.utcoffset() == timedelta(0)

    def test_vendor_rows_carry_an_aware_utc_timestamp(self, tmp_path):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        offsets = {
            parse_fetched_at(row["fetched_at"]).utcoffset()
            for row in _all_rows(cache)
            if row["source"] == "vendor"
        }
        assert offsets == {timedelta(0)}

    def test_a_naive_row_is_read_as_utc(self):
        assert parse_fetched_at("2026-01-15T08:00:00") == NOW

    def test_an_aware_row_keeps_its_offset(self):
        east = "2026-01-15T13:00:00+05:00"
        assert parse_fetched_at(east) == NOW

    def test_an_unreadable_value_parses_to_nothing(self):
        assert parse_fetched_at("") is None
        assert parse_fetched_at("whenever") is None


class TestIsStale:
    """`is_stale` compares against UTC, whichever form the stored row has."""

    def test_a_naive_row_is_read_as_utc(self, tmp_path, frozen_now):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-07T08:00:00"))
        assert cache.is_stale("test", "Probe")

    def test_a_fresh_naive_row_is_not_stale(self, tmp_path, frozen_now):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-15T07:00:00"))
        assert not cache.is_stale("test", "Probe")

    def test_a_row_written_in_another_offset_is_not_stale(
        self, tmp_path, frozen_now
    ):
        """08:00+05:00 is the same instant as the frozen 08:00 UTC, so fresh."""
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-15T13:00:00+05:00"))
        assert not cache.is_stale("test", "Probe")

    def test_an_unreadable_timestamp_is_stale(self, tmp_path, frozen_now):
        """A row nobody can date can't be shown to be fresh."""
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "whenever"))
        assert cache.is_stale("test", "Probe")

    def test_a_future_row_reads_fresh_from_both_readers(
        self, tmp_path, frozen_now
    ):
        """A clock-skewed row can't be stale to one reader and fresh to another.

        ``is_stale`` and ``status`` must agree on the same row, so both read
        the age from one non-negative helper rather than each subtracting
        the instants themselves.
        """
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-15T09:00:00+00:00"))

        assert not cache.is_stale("test", "Probe")
        assert cache.status()["sources"]["infracost"]["ageHours"] == 0.0

    def test_the_newest_row_is_picked_by_instant_not_by_text(
        self, tmp_path, frozen_now
    ):
        """A text sort picks the wrong row when two offsets disagree.

        ``12:00+05:00`` sorts above ``09:00+00:00`` but names the earlier
        instant (07:00 UTC against 09:00 UTC), so a text ``MAX()`` reports a
        seven-day-old row as the newest and calls a fresh cache stale.
        """
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-08T12:00:00+05:00", "M1"))
        cache.upsert(_price("infracost", "2026-01-08T09:00:00+00:00", "M2"))
        assert not cache.is_stale("test", "Probe")


class TestStatus:
    """`PricingCache.status` reports rows, newest row and age per source."""

    def test_it_reports_rows_newest_and_age_per_source(
        self, tmp_path, frozen_now
    ):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-15T07:00:00+00:00", "M1"))
        cache.upsert(_price("infracost", "2026-01-15T04:00:00+00:00", "M2"))
        cache.upsert(_price("seed", "2026-01-10T12:00:00+00:00"))

        status = cache.status()

        assert status["path"] == str(tmp_path / "pricing.db")
        assert status["sources"]["infracost"] == {
            "rows": 2,
            "newest": "2026-01-15T07:00:00+00:00",
            "ageHours": 1.0,
        }
        assert status["sources"]["seed"] == {
            "rows": 1,
            "newest": "2026-01-10T12:00:00+00:00",
            "ageHours": 116.0,
        }

    def test_a_source_without_a_readable_timestamp_reports_no_age(
        self, tmp_path, frozen_now
    ):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "whenever"))

        entry = cache.status()["sources"]["infracost"]

        assert entry == {"rows": 1, "newest": None, "ageHours": None}

    def test_a_naive_row_is_reported_with_its_utc_reading(
        self, tmp_path, frozen_now
    ):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-14T08:00:00"))

        assert cache.status()["sources"]["infracost"]["ageHours"] == 24.0

    def test_a_future_timestamp_reports_no_age(self, tmp_path, frozen_now):
        """A clock-skewed row reads as new, not as a negative age."""
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2030-01-01T00:00:00+00:00"))

        assert cache.status()["sources"]["infracost"]["ageHours"] == 0.0

    def test_the_age_follows_utc_not_the_local_clock(
        self, tmp_path, monkeypatch
    ):
        """The same row ages the same whatever zone the reader runs in.

        ``_utc_now`` is left real, and only ``datetime`` is substituted, so
        the assertion turns on which clock ``cache`` reads. The faked wall
        clock runs nine hours ahead of UTC, so a reader that took the naive
        local value would call the four-hour-old row thirteen hours old (or
        fail outright, comparing a naive instant with an aware one). The
        clock is faked in-process, so the test needs no ``time.tzset`` and
        runs on Windows too (#447).
        """
        monkeypatch.setattr(cache_module, "datetime", _LocalSkewedClock)
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-15T04:00:00"))
        assert cache.status()["sources"]["infracost"]["ageHours"] == 4.0


class TestLiveAgeHours:
    """Only the sources a sync fetched drive the freshness gate."""

    def test_it_is_the_youngest_fetched_row(self, tmp_path, frozen_now):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-15T07:00:00+00:00"))
        cache.upsert(_price("azure-retail", "2026-01-15T06:00:00+00:00"))
        cache.upsert(_price("aws-pricelist", "2026-01-15T02:00:00+00:00"))

        assert live_age_hours(cache.status()) == 1.0

    def test_it_ignores_the_bundled_offline_rows(self, tmp_path, frozen_now):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("infracost", "2026-01-15T07:00:00+00:00"))
        cache.upsert(_price("seed", "2020-01-01T00:00:00+00:00"))
        cache.upsert(_price("vendor", "2020-01-01T00:00:00+00:00"))

        assert live_age_hours(cache.status()) == 1.0

    def test_it_is_none_when_nothing_was_fetched(self, tmp_path, frozen_now):
        cache = PricingCache(db_path=tmp_path / "pricing.db")
        cache.upsert(_price("seed", "2020-01-01T00:00:00+00:00"))

        assert live_age_hours(cache.status()) is None


class TestPricingStatusCommand:
    """`pricing-status` reports the cache and can gate a run on its age."""

    @pytest.fixture
    def cache_at(self, tmp_path, monkeypatch, frozen_now):
        """Point the command at a cache in the test's own directory."""
        path = tmp_path / "pricing.db"
        monkeypatch.setattr(cache_module, "DB_PATH", path)
        return PricingCache(db_path=path)

    def test_json_output_names_the_path_and_the_sources(
        self, cache_at, capsys
    ):
        cache_at.upsert(_price("infracost", "2026-01-15T07:00:00+00:00"))

        assert main(["pricing-status", "--json"]) == 0

        report = json.loads(capsys.readouterr().out)
        assert report["path"] == str(cache_at.db_path)
        assert report["sources"]["infracost"] == {
            "rows": 1, "newest": "2026-01-15T07:00:00+00:00", "ageHours": 1.0}

    def test_text_output_reports_every_source(self, cache_at, capsys):
        cache_at.upsert(_price("infracost", "2026-01-15T07:00:00+00:00"))

        assert main(["pricing-status"]) == 0

        out = capsys.readouterr().out
        assert str(cache_at.db_path) in out
        assert "infracost" in out
        assert "1.0h" in out

    def test_max_age_hours_passes_on_a_fresh_cache(self, cache_at, capsys):
        cache_at.upsert(_price("infracost", "2026-01-15T07:00:00+00:00"))

        assert main(["pricing-status", "--json", "--max-age-hours", "24"]) == 0

        assert json.loads(capsys.readouterr().out)["stale"] is False

    def test_max_age_hours_fails_on_a_stale_cache(self, cache_at, capsys):
        cache_at.upsert(_price("infracost", "2026-01-14T07:00:00+00:00"))

        assert main(["pricing-status", "--json", "--max-age-hours", "24"]) == 1

        assert json.loads(capsys.readouterr().out)["stale"] is True

    def test_text_output_agrees_with_the_gate(self, cache_at, capsys):
        """The line a human reads says what the exit code says (#447).

        The gate verdict is printed only when it was computed, so the text
        report and the status agree on both sides of the limit.
        """
        cache_at.upsert(_price("infracost", "2026-01-14T07:00:00+00:00"))

        assert main(["pricing-status", "--max-age-hours", "24"]) == 1
        assert "stale: yes (max age 24.0h)" in capsys.readouterr().out

        cache_at.upsert(_price("infracost", "2026-01-15T07:00:00+00:00", "M2"))

        assert main(["pricing-status", "--max-age-hours", "24"]) == 0
        assert "stale: no (max age 24.0h)" in capsys.readouterr().out

    def test_max_age_hours_fails_when_nothing_was_fetched(
        self, cache_at, capsys
    ):
        cache_at.upsert(_price("seed", "2020-01-01T00:00:00+00:00"))

        assert main(["pricing-status", "--json", "--max-age-hours", "24"]) == 1

        assert "no fetched prices" in capsys.readouterr().err

    def test_without_the_flag_the_command_never_fails(self, cache_at):
        cache_at.upsert(_price("seed", "2020-01-01T00:00:00+00:00"))

        assert main(["pricing-status", "--json"]) == 0


def _all_rows(cache: PricingCache) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(cache.db_path)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT source, fetched_at FROM prices")]
    finally:
        conn.close()


class _LocalSkewedClock(datetime):
    """A clock whose local time runs nine hours ahead of UTC.

    ``cache`` compares against UTC through ``datetime.now(timezone.utc)``; a
    reader that reached for the naive local value instead would age every row
    by the zone's offset. Substituting this class in place of ``datetime``
    proves which one the cache reads, without the process-wide ``TZ`` variable
    and ``time.tzset`` that only exist on Unix.
    """

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            # Arithmetic, not `NOW.hour + 9`, so an edit to NOW can't push the
            # hour past 23 and raise (#447).
            local = (NOW + timedelta(hours=9)).replace(tzinfo=None)
            return cls(local.year, local.month, local.day,
                       local.hour, local.minute)
        return NOW.astimezone(tz)
