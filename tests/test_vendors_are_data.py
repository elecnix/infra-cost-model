"""The last steps of Epic #246: vendors are data, not code.

Three shapes (`free_tier`, `per_unit_flat`, `flat_subscription`) restated what
a tiered price row already says, so they are gone and their prices live in
`infra_cost_model/vendors/<id>/prices.yaml`. The entry-point group that let an
installed package add shapes is gone too. Each vendor directory's id is the
`vendor` its rows use, so a provider id that validates always has prices.
"""

import shutil
from importlib import resources
from pathlib import Path

import pytest
import yaml

from infra_cost_model.cli import main
from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.vendors import _read_vendor_prices
from infra_cost_model.saas import SaaSPricingRegistry
from infra_cost_model.saas import pricing_shapes
from infra_cost_model.schema.cost_model_schema import KNOWN_PROVIDERS, validate_cost_model

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDORS_PACKAGE = "infra_cost_model.vendors"
REMOVED_SHAPES = ["free_tier", "per_unit_flat", "flat_subscription"]


def _model(metric, provider="workos"):
    return {
        "version": "1.0",
        "workflow": {"name": "t", "entry": "n", "frequency": {"unit": "perMonth", "value": 1}},
        "nodes": {"n": {"nodeType": "external", "resourceAddress": "n", "provider": provider,
                        "service": "WorkOS", "region": "global",
                        "usageMetrics": {"m": metric}}},
    }


class TestRemovedShapes:
    @pytest.mark.parametrize("shape", REMOVED_SHAPES)
    def test_validate_points_to_vendor_price_rows(self, shape):
        errors = validate_cost_model(_model({"unit": "u", "value": 1, "fixed": True, "shape": shape}))
        message = [e for e in errors if shape in e]
        assert message, errors
        assert "vendor price rows" in message[0]
        assert "infra_cost_model/vendors/<id>/prices.yaml" in message[0]

    @pytest.mark.parametrize("shape", REMOVED_SHAPES)
    def test_compute_refuses_a_removed_shape(self, shape):
        with pytest.raises(ValueError, match="vendor price rows"):
            SaaSPricingRegistry.compute(shape, 1, {})

    def test_only_transactional_is_left(self):
        assert SaaSPricingRegistry.known_shapes() == {"transactional"}

    @pytest.mark.parametrize(
        "param", ["rate", "free", "overage", "tiers", "subscription", "includedCredits", "creditValue"]
    )
    def test_parameters_of_removed_shapes_are_unknown(self, param):
        metric = {"unit": "u", "value": 1, "shape": "transactional", param: 1}
        errors = validate_cost_model(_model(metric))
        assert any(param in e for e in errors), errors

    def test_schema_lists_only_transactional(self):
        import json

        from infra_cost_model.schema.cost_model_schema import SCHEMA_PATH

        schema = json.loads(SCHEMA_PATH.read_text())
        shape = schema["definitions"]["usageMetric"]["properties"]["shape"]
        assert shape["enum"] == ["transactional"]


class TestNoEntryPointGroup:
    def test_discovery_function_is_gone(self):
        assert not hasattr(pricing_shapes, "discover_entry_point_handlers")

    def test_pyproject_declares_no_saas_handlers_group(self):
        assert "saas_handlers" not in (REPO_ROOT / "pyproject.toml").read_text()


def _vendor_dirs():
    root = resources.files(VENDORS_PACKAGE)
    return sorted(
        (d for d in root.iterdir()
         if d.is_dir() and not d.name.startswith("_") and d.joinpath("prices.yaml").is_file()),
        key=lambda d: d.name,
    )


class TestVendorIdentity:
    @pytest.mark.parametrize("vendor_dir", _vendor_dirs(), ids=lambda d: d.name)
    def test_manifest_id_is_the_directory_name(self, vendor_dir):
        manifest = yaml.safe_load(vendor_dir.joinpath("vendor.yaml").read_text())
        assert manifest["id"] == vendor_dir.name

    @pytest.mark.parametrize("vendor_dir", _vendor_dirs(), ids=lambda d: d.name)
    def test_every_row_uses_the_directory_id(self, vendor_dir):
        rows = yaml.safe_load(vendor_dir.joinpath("prices.yaml").read_text())
        assert {row["vendor"] for row in rows} == {vendor_dir.name}

    def test_github_copilot_is_not_a_provider(self):
        assert "github" in KNOWN_PROVIDERS
        assert "github-copilot" not in KNOWN_PROVIDERS

    def test_every_known_vendor_provider_has_rows(self):
        vendors_with_rows = {p.vendor for p in _read_vendor_prices()}
        vendor_ids = {d.name for d in _vendor_dirs()}
        assert vendor_ids <= vendors_with_rows

    def test_loader_rejects_a_directory_without_rows_for_its_id(self, monkeypatch, tmp_path):
        copy = tmp_path / "vendors"
        shutil.copytree(resources.files(VENDORS_PACKAGE), copy)
        prices = copy / "workos" / "prices.yaml"
        prices.write_text(prices.read_text().replace("vendor: workos", "vendor: workos-inc"))
        original = resources.files
        monkeypatch.setattr(
            resources, "files",
            lambda package: copy if package == VENDORS_PACKAGE else original(package),
        )
        with pytest.raises(ValueError, match=r"infra_cost_model/vendors/workos/prices.yaml.*'workos'"):
            _read_vendor_prices()


class TestSaasExampleOnVendorRows:
    EXAMPLE = REPO_ROOT / "examples" / "saas-subscription-api.yaml"

    def test_example_validates(self):
        assert main(["validate", str(self.EXAMPLE)]) == 0

    def test_example_uses_no_removed_shape(self):
        text = self.EXAMPLE.read_text()
        assert not any(f"shape: {s}" in text for s in REMOVED_SHAPES)

    def test_vendor_nodes_price_from_rows(self, seed_catalog):
        model = yaml.safe_load(self.EXAMPLE.read_text())
        costs = CostEngine(model, catalog=seed_catalog, time_basis="monthly").compute()
        # WorkOS: 900,000 MAU inside the free 1,000,000; 2 SSO connections at
        # $125; one custom domain at $99. Datadog: 6 Enterprise hosts at $23.
        assert costs["workos_identity"] == pytest.approx(2 * 125.0 + 99.0)
        assert costs["datadog_observability"] == pytest.approx(6 * 23.0)

    def test_yearly_is_twelve_months(self, seed_catalog):
        model = yaml.safe_load(self.EXAMPLE.read_text())
        monthly = CostEngine(model, catalog=seed_catalog, time_basis="monthly").compute()
        yearly = CostEngine(model, catalog=seed_catalog, time_basis="yearly").compute()
        for node in ("workos_identity", "datadog_observability"):
            assert yearly[node] == pytest.approx(12 * monthly[node])


class TestWorkosBands:
    """Adjacent rows share a boundary: the end is exclusive, the start inclusive."""

    def _cost(self, seed_catalog, metric, quantity):
        model = _model({})
        model["nodes"]["n"]["usageMetrics"] = {metric: {"unit": "u", "value": quantity, "fixed": True}}
        return CostEngine(model, catalog=seed_catalog, time_basis="monthly").compute()["n"]

    def test_fifteen_sso_connections_are_all_in_the_first_band(self, seed_catalog):
        assert self._cost(seed_catalog, "SSO-Connection", 15) == pytest.approx(15 * 125.0)

    def test_the_sixteenth_connection_is_in_the_second_band(self, seed_catalog):
        assert self._cost(seed_catalog, "SSO-Connection", 16) == pytest.approx(15 * 125.0 + 100.0)

    def test_one_million_mau_are_free(self, seed_catalog):
        assert self._cost(seed_catalog, "AuthKit-MAU", 1_000_000) == pytest.approx(0.0)


def test_github_copilot_example_stays_600(seed_catalog):
    model = yaml.safe_load((REPO_ROOT / "examples" / "github-copilot.yaml").read_text())
    total = sum(CostEngine(model, catalog=seed_catalog, time_basis="monthly").compute().values())
    assert total == pytest.approx(600.0)
