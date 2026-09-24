"""Azure Resource Manager (ARM) template ingestion (#222)."""
import json
import os
import tempfile
import warnings
from pathlib import Path

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.resources.registry import extract_resources_from_arm

FIXTURE = Path(__file__).parent / "fixtures" / "arm" / "web-api.json"

APIM = "Microsoft.ApiManagement/service:apim-orders"
FUNC = "Microsoft.Web/sites:func-orders"
COSMOS = "Microsoft.DocumentDB/databaseAccounts:cosmos-orders"
BLOB = "Microsoft.Storage/storageAccounts:storders"
OPENAI = "Microsoft.CognitiveServices/accounts:oai-orders"


def load_fixture() -> dict:
    with open(FIXTURE) as f:
        return json.load(f)


def extract_fixture() -> tuple[dict, list[str]]:
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        nodes = extract_resources_from_arm(load_fixture())
    return nodes, [str(w.message) for w in record]


class TestExtract:
    def test_priced_resources_are_extracted(self):
        nodes, _ = extract_fixture()
        assert set(nodes) == {APIM, FUNC, COSMOS, BLOB, OPENAI}

    def test_every_node_is_azure_with_its_service(self):
        nodes, _ = extract_fixture()
        services = {addr: n["service"] for addr, n in nodes.items()}
        assert services == {
            APIM: "APIManagement",
            FUNC: "AzureFunctions",
            COSMOS: "CosmosDB",
            BLOB: "BlobStorage",
            OPENAI: "AzureOpenAI",
        }
        assert {n["provider"] for n in nodes.values()} == {"azure"}
        assert all(n["resourceAddress"] == addr for addr, n in nodes.items())

    def test_node_types(self):
        nodes, _ = extract_fixture()
        assert nodes[APIM]["nodeType"] == "routing"
        assert nodes[FUNC]["nodeType"] == "compute"
        assert nodes[COSMOS]["nodeType"] == "storage"
        assert nodes[BLOB]["nodeType"] == "storage"
        assert nodes[OPENAI]["nodeType"] == "compute"

    def test_parameter_default_resolves_region_and_name(self):
        """`[parameters('x')]` resolves to the template parameter's default."""
        nodes, _ = extract_fixture()
        assert nodes[APIM]["region"] == "eastus"
        assert nodes[FUNC]["region"] == "eastus"
        assert nodes[COSMOS]["region"] == "eastus"

    def test_config_reads_arm_properties(self):
        nodes, _ = extract_fixture()
        assert nodes[APIM]["config"]["skuName"] == "Consumption"
        assert nodes[APIM]["config"]["publisherName"] == "Orders Team"
        assert nodes[FUNC]["config"]["runtime"] == "python"
        assert nodes[COSMOS]["config"]["offerType"] == "Standard"
        assert nodes[COSMOS]["config"]["consistencyLevel"] == "Session"
        assert nodes[BLOB]["config"] == {
            "accountTier": "Standard", "replicationType": "LRS", "accessTier": "Hot"}
        assert nodes[OPENAI]["config"] == {"kind": "OpenAI", "skuName": "S0"}

    def test_child_resources_warn_as_unsupported(self):
        """Top-level and nested children are not priced as their parent."""
        _, messages = extract_fixture()
        unsupported = [m for m in messages if "could not be extracted" in m]
        assert len(unsupported) == 1
        message = unsupported[0]
        assert "Microsoft.Web/sites/slots:func-orders/staging" in message
        assert "Microsoft.Storage/storageAccounts/blobServices:storders/default" in message
        # Nested children get the parent's type and name as a prefix.
        assert "Microsoft.ApiManagement/service/apis:apim-orders/orders" in message
        assert "Microsoft.DocumentDB/databaseAccounts/sqlDatabases:cosmos-orders/orders" in message
        assert "Microsoft.Web/serverfarms:plan-orders" in message


class TestRegionExpressions:
    def template(self, location, parameters=None) -> dict:
        return {
            "parameters": parameters or {},
            "resources": [{
                "type": "Microsoft.Storage/storageAccounts",
                "name": "st1",
                "location": location,
                "sku": {"name": "Standard_GRS"},
            }],
        }

    def test_resource_group_location_resolves_to_none_with_warning(self):
        with pytest.warns(UserWarning, match=r"resourceGroup\(\)\.location"):
            nodes = extract_resources_from_arm(
                self.template("[resourceGroup().location]"))
        assert nodes["Microsoft.Storage/storageAccounts:st1"]["region"] is None

    def test_parameter_without_default_resolves_to_none_with_warning(self):
        with pytest.warns(UserWarning, match="location"):
            nodes = extract_resources_from_arm(self.template(
                "[parameters('location')]", {"location": {"type": "string"}}))
        assert nodes["Microsoft.Storage/storageAccounts:st1"]["region"] is None

    def test_parameter_default_that_is_an_expression_resolves_to_none(self):
        params = {"location": {"type": "string",
                               "defaultValue": "[resourceGroup().location]"}}
        with pytest.warns(UserWarning):
            nodes = extract_resources_from_arm(
                self.template("[parameters('location')]", params))
        assert nodes["Microsoft.Storage/storageAccounts:st1"]["region"] is None

    def test_literal_location_needs_no_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            nodes = extract_resources_from_arm(self.template("westeurope"))
        assert nodes["Microsoft.Storage/storageAccounts:st1"]["region"] == "westeurope"


class TestInputShapes:
    def test_symbolic_name_resources_object(self):
        """languageVersion 2.0 templates key resources by symbolic name."""
        template = {"languageVersion": "2.0", "resources": {
            "st": {"type": "Microsoft.Storage/storageAccounts", "name": "st1",
                   "location": "eastus", "sku": {"name": "Standard_LRS"}}}}
        nodes = extract_resources_from_arm(template)
        assert list(nodes) == ["Microsoft.Storage/storageAccounts:st1"]

    def test_deployment_export_wraps_template(self):
        """`az deployment group export` output nests the template under `template`."""
        nodes = extract_resources_from_arm({"template": load_fixture()})
        assert FUNC in nodes

    def test_empty_template(self):
        assert extract_resources_from_arm({}) == {}


class TestCli:
    def test_extract_from_arm(self, capsys):
        from infra_cost_model.cli import main
        assert main(["extract", str(FIXTURE), "--from", "arm", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert set(out) == {APIM, FUNC, COSMOS, BLOB, OPENAI}


def build_model(nodes: dict) -> dict:
    """Wire the extracted nodes into a request path with usage per request."""
    usage = {
        APIM: {"requests": {"unit": "requests", "value": 1}},
        FUNC: {"invocations": {"unit": "requests", "value": 1},
               "avgDurationMs": {"unit": "ms", "value": 100},
               "memoryMb": {"unit": "MB", "value": 512}},
        COSMOS: {"readRequests": {"unit": "requests", "value": 2}},
        BLOB: {"writeRequests": {"unit": "requests", "value": 1}},
        OPENAI: {"inputTokens": {"unit": "tokens", "value": 500},
                 "outputTokens": {"unit": "tokens", "value": 200}},
    }
    model_nodes = {}
    for addr, node in nodes.items():
        model_nodes[addr] = {
            "nodeType": node["nodeType"],
            "resourceAddress": addr,
            "provider": node["provider"],
            "service": node["service"],
            "region": node["region"],
            "usageMetrics": usage[addr],
        }
    return {
        "version": "1.0",
        "workflow": {"name": "arm-web-api", "entry": APIM,
                     "frequency": {"unit": "perMinute", "value": 100}},
        "nodes": model_nodes,
        "edges": [
            {"from": APIM, "to": FUNC, "rate": 1},
            {"from": FUNC, "to": COSMOS, "rate": 1},
            {"from": FUNC, "to": BLOB, "rate": 1},
            {"from": FUNC, "to": OPENAI, "rate": 0.5},
        ],
    }


class TestCost:
    def test_extracted_model_computes_with_seed_catalog(self, seed_catalog):
        nodes, _ = extract_fixture()
        model = build_model(nodes)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            costs = CostEngine(model, catalog=seed_catalog, time_basis="monthly").compute()
        for addr in model["nodes"]:
            assert addr in costs, f"Node '{addr}' missing from costs"

    def test_unpriced_azure_metrics_are_reported(self, seed_catalog):
        """The seed has no Azure rows, so the engine names each metric it left out."""
        nodes, _ = extract_fixture()
        engine = CostEngine(build_model(nodes), catalog=seed_catalog, time_basis="monthly")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            engine.compute()
        assert {u.provider for u in engine.unpriced_metrics} == {"azure"}

    @pytest.mark.xfail(strict=True, reason="The seed has no Azure rows yet (#363)")
    def test_extracted_model_has_a_price(self, seed_catalog):
        nodes, _ = extract_fixture()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            costs = CostEngine(build_model(nodes), catalog=seed_catalog,
                               time_basis="monthly").compute()
        assert sum(costs.values()) > 0
