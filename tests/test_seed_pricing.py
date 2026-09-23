"""`seed-pricing` loads every service in the seed file (issue #293)."""

import json
import sqlite3

import pytest
import yaml

from infra_cost_model.cli import main
from infra_cost_model.pricing import cache as cache_module
from infra_cost_model.pricing.cache import SEED_PRICES_PATH

# The cache also holds vendor rows that load with it, so the checks look
# only at rows that came from the seed file.
SEED_ROWS = "source IN ('seed', 'seed-initial')"


@pytest.fixture
def empty_cache(monkeypatch, tmp_path):
    """Point the default cache at an empty database for this test."""
    db_path = tmp_path / "prices.db"
    monkeypatch.setattr(cache_module, "DB_PATH", db_path)
    return db_path


def seed_services():
    rows = json.loads(SEED_PRICES_PATH.read_text())
    return {(row["vendor"], row["service"]) for row in rows}


def cached_services(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return set(conn.execute(
            f"SELECT DISTINCT vendor, service FROM prices WHERE {SEED_ROWS}"))
    finally:
        conn.close()


def test_seed_pricing_loads_every_seed_service(empty_cache, capsys):
    assert main(["seed-pricing"]) == 0
    missing = seed_services() - cached_services(empty_cache)
    assert missing == set()


def test_seed_pricing_twice_keeps_one_copy_of_each_row(empty_cache, capsys):
    assert main(["seed-pricing"]) == 0
    assert main(["seed-pricing"]) == 0
    conn = sqlite3.connect(empty_cache)
    try:
        (count,) = conn.execute(
            f"SELECT COUNT(*) FROM prices WHERE {SEED_ROWS}").fetchone()
    finally:
        conn.close()
    assert count == len(json.loads(SEED_PRICES_PATH.read_text()))


def test_seed_pricing_with_services_loads_only_those(empty_cache, capsys):
    assert main(["seed-pricing", "AmazonSQS"]) == 0
    assert cached_services(empty_cache) == {("aws", "AmazonSQS")}


def test_compute_prices_sqs_after_seed_pricing(empty_cache, tmp_path, capsys):
    model = {
        "version": "1.0",
        "workflow": {"name": "w", "entry": "q",
                     "frequency": {"unit": "perMonth", "value": 5_000_000}},
        "nodes": {"q": {
            "nodeType": "routing",
            "provider": "aws",
            "service": "AmazonSQS",
            "region": "us-east-1",
            "usageMetrics": {"SQS-Standard-Request": {"unit": "requests", "value": 1}},
        }},
        "edges": [],
    }
    path = tmp_path / "sqs.yaml"
    path.write_text(yaml.safe_dump(model))

    assert main(["seed-pricing"]) == 0
    capsys.readouterr()
    assert main(["compute", "--time-basis", "monthly", str(path)]) == 0
    assert "Total Monthly Cost: $1.600000" in capsys.readouterr().out
