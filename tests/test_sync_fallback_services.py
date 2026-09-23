"""The seed fallback of `sync-pricing` covers every seed service (issue #303)."""

import json
import sqlite3

import pytest
import yaml

from infra_cost_model.cli import main
from infra_cost_model.pricing import cache as cache_module
from infra_cost_model.pricing.cache import SEED_PRICES_PATH, PricingCache
from infra_cost_model.pricing.sources import aws_pricing
from infra_cost_model.pricing.sources.aws_pricing import aws_fallback_prices

SEED_ROWS = "source IN ('seed', 'seed-initial')"


@pytest.fixture
def requested(monkeypatch):
    """Replace the AWS Price List call with a stub and record each service code."""
    calls: list[str] = []

    def fake_fetch(service_code):
        calls.append(service_code)
        return [{
            "sku": f"{service_code}-sku",
            "service": service_code,
            "attributes": {"regionCode": "us-east-1", "operation": "Invoke request"},
            "unit": "Requests",
            "price_usd": 0.2e-6,
        }]

    monkeypatch.setattr(aws_pricing, "fetch_aws_price_list", fake_fetch)
    return calls


@pytest.fixture
def no_infracost_key(monkeypatch, tmp_path):
    """Remove every Infracost credential: no key and no CLI session."""
    monkeypatch.delenv("INFRACOST_API_KEY", raising=False)
    monkeypatch.setenv("INFRACOST_CONFIG_DIR", str(tmp_path / "infracost"))


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


def sqs_model(tmp_path):
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
    return path


def test_sync_pricing_without_key_loads_every_seed_service(
        no_infracost_key, empty_cache, requested, capsys):
    assert main(["sync-pricing"]) == 0
    assert seed_services() - cached_services(empty_cache) == set()


def test_compute_prices_sqs_after_sync_pricing_without_key(
        no_infracost_key, empty_cache, requested, tmp_path, capsys):
    path = sqs_model(tmp_path)
    assert main(["sync-pricing"]) == 0
    capsys.readouterr()
    assert main(["compute", "--time-basis", "monthly", str(path)]) == 0
    assert "Total Monthly Cost: $1.600000" in capsys.readouterr().out


def test_unknown_service_is_skipped_and_known_service_is_fetched(
        monkeypatch, requested, tmp_path):
    monkeypatch.setattr(cache_module, "SEED_PRICES_PATH", tmp_path / "missing.json")
    cache = PricingCache(db_path=tmp_path / "prices.db")
    with pytest.warns(UserWarning, match="NotAnAwsService"):
        count = aws_fallback_prices(["AWSLambda", "NotAnAwsService"], cache)
    assert requested == ["AWSLambda"]
    assert count > 0


def test_unknown_service_alone_is_never_fetched(monkeypatch, requested, tmp_path):
    monkeypatch.setattr(cache_module, "SEED_PRICES_PATH", tmp_path / "missing.json")
    cache = PricingCache(db_path=tmp_path / "prices.db")
    with pytest.warns(UserWarning, match="NotAnAwsService"):
        with pytest.raises(RuntimeError):
            aws_fallback_prices(["NotAnAwsService"], cache)
    assert requested == []
