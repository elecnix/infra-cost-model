"""Azure OpenAI tokens are priced by model and deployment type (#371).

Azure prices tokens per model and per deployment type (Global Standard, Data
Zone Standard or regional Standard). The model is set on the deployment
resource, not on the account, so each deployment is a node of its own, and
its `config` names the model and the deployment type. The handler picks the
catalog rows of that model and type.
"""
import json
import warnings

import pytest

from infra_cost_model.engine.engine import CostEngine
from infra_cost_model.pricing.cache import SEED_PRICES_PATH
from infra_cost_model.pricing.sources import infracost as ic
from infra_cost_model.resources.azure import (
    AzureOpenAI, AzureOpenAIDeployment, OPENAI_MODELS, matches_arm_type,
)
from infra_cost_model.resources.registry import (
    ResourceRegistry, extract_resources_from_arm, extract_resources_from_pulumi,
    extract_resources_from_tf,
)
from infra_cost_model.schema.cost_model_schema import validate_cost_model

REGION = "eastus"
RG = "/subscriptions/0000/resourceGroups/rg/providers"
ACCOUNT_ID = f"{RG}/Microsoft.CognitiveServices/accounts/oai"


# --- Catalog metrics ----------------------------------------------------------


def test_node_without_a_model_prices_gpt_4o_global_standard():
    assert AzureOpenAI().catalog_metrics_for({}) == {
        "inputTokens": "AzureOpenAI-gpt-4o-Global-Input-Token",
        "cachedReadTokens": "AzureOpenAI-gpt-4o-Global-Cached-Input-Token",
        "outputTokens": "AzureOpenAI-gpt-4o-Global-Output-Token",
    }
    assert AzureOpenAI().catalog_metrics == AzureOpenAI().catalog_metrics_for({})


@pytest.mark.parametrize("deployment_type,tier", [
    ("GlobalStandard", "Global"), ("DataZoneStandard", "DataZone"),
    ("Standard", "Regional"), ("standard", "Regional"),
])
def test_deployment_type_selects_the_tier(deployment_type, tier):
    metrics = AzureOpenAI().catalog_metrics_for(
        {"model": "gpt-4.1-mini", "deploymentType": deployment_type})
    assert metrics["inputTokens"] == f"AzureOpenAI-gpt-4.1-mini-{tier}-Input-Token"


def test_model_name_is_case_insensitive():
    metrics = AzureOpenAI().catalog_metrics_for({"model": "GPT-4o-Mini"})
    assert metrics["outputTokens"] == "AzureOpenAI-gpt-4o-mini-Global-Output-Token"


def test_gpt_4o_2024_11_20_shares_the_2024_08_06_prices():
    metrics = AzureOpenAI().catalog_metrics_for(
        {"model": "gpt-4o", "modelVersion": "2024-11-20"})
    assert metrics["inputTokens"] == "AzureOpenAI-gpt-4o-Global-Input-Token"


def test_a_version_with_other_prices_gets_a_metric_of_its_own():
    # GPT-4o 2024-05-13 costs twice as much, and has no seed rows.
    metrics = AzureOpenAI().catalog_metrics_for(
        {"model": "gpt-4o", "modelVersion": "2024-05-13"})
    assert metrics["inputTokens"] == "AzureOpenAI-gpt-4o-2024-05-13-Global-Input-Token"


def seeded_metrics():
    rows = json.loads(SEED_PRICES_PATH.read_text())
    return {r["usage_metric"] for r in rows if r["service"] == "AzureOpenAI"
            and r["region"] == REGION}


@pytest.mark.parametrize("model", sorted(OPENAI_MODELS))
def test_every_supported_model_has_seed_rows(model):
    seeded = seeded_metrics()
    for tier in OPENAI_MODELS[model].tiers:
        metrics = AzureOpenAI().catalog_metrics_for(
            {"model": model, "deploymentType": tier})
        assert metrics["inputTokens"] in seeded
        if OPENAI_MODELS[model].output:
            assert metrics["outputTokens"] in seeded
            assert metrics["cachedReadTokens"] in seeded


def test_every_openai_seed_row_has_an_infracost_descriptor():
    for metric in seeded_metrics():
        descriptor = ic.METRIC_DESCRIPTORS[metric]
        assert descriptor["vendor"] == "azure"
        assert descriptor["service"] == "Foundry Models"
        assert descriptor["store_service"] == "AzureOpenAI"
        assert descriptor["store_unit"] == "tokens"
        filters = {f["key"]: f["value"] for f in descriptor["attribute_filters"]}
        assert filters["productName"] == "Azure OpenAI"
        assert filters["meterName"].endswith(" Tokens")


def test_descriptor_stores_one_token_prices():
    """Infracost prices tokens per 1K; the stored row prices one token."""
    metric = "AzureOpenAI-gpt-4.1-nano-Global-Input-Token"
    descriptor = ic.METRIC_DESCRIPTORS[metric]
    assert descriptor["unit"] == "1K"
    assert descriptor["unit_scale"] == 1000


# --- Pricing ------------------------------------------------------------------


def openai_model(config, usage, per_month=1000, pricing_model="token_based"):
    address = "azurerm_cognitive_deployment.chat"
    node = {
        "nodeType": "compute", "resourceAddress": address, "provider": "azure",
        "service": "AzureOpenAI", "region": REGION, "pricingModel": pricing_model,
        "usageMetrics": usage,
    }
    if config is not None:
        node["config"] = config
    return address, {
        "version": "1.0",
        "workflow": {"name": "chat", "entry": address,
                     "frequency": {"unit": "perMonth", "value": per_month}},
        "nodes": {address: node},
        "edges": [],
    }


def compute(model, catalog):
    engine = CostEngine(model, catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs, engine


TOKENS = {"inputTokens": {"unit": "tokens", "value": 1000},
          "outputTokens": {"unit": "tokens", "value": 500}}


@pytest.mark.parametrize("pricing_model", ["token_based", "flat"])
def test_node_config_picks_the_model_rows(seed_catalog, pricing_model):
    # 1M input tokens at $0.44/M and 500K output tokens at $1.76/M
    # (GPT-4.1 mini, Data Zone Standard).
    address, model = openai_model(
        {"model": "gpt-4.1-mini", "deploymentType": "DataZoneStandard"}, TOKENS,
        pricing_model=pricing_model)
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(0.44 + 0.88)
    assert engine.unpriced_metrics == []


def test_cached_input_tokens_are_priced(seed_catalog):
    # 1M cached input tokens of o3 in a Global Standard deployment: $0.50.
    address, model = openai_model(
        {"model": "o3", "deploymentType": "GlobalStandard"},
        {"cachedReadTokens": {"unit": "tokens", "value": 1000}})
    costs, _ = compute(model, seed_catalog)
    assert costs[address] == pytest.approx(0.50)


def test_node_without_config_keeps_the_gpt_4o_prices(seed_catalog):
    address, model = openai_model(None, TOKENS)
    costs, _ = compute(model, seed_catalog)
    # $2.50/M input and $10/M output.
    assert costs[address] == pytest.approx(2.5 + 5.0)


def test_unknown_model_is_reported_unpriced(seed_catalog):
    address, model = openai_model({"model": "gpt-99"}, TOKENS)
    costs, engine = compute(model, seed_catalog)
    assert costs[address] == 0.0
    assert {m.metric for m in engine.unpriced_metrics} == {"inputTokens", "outputTokens"}


def test_schema_accepts_node_config():
    _, model = openai_model({"model": "gpt-4o"}, TOKENS)
    assert validate_cost_model(model) == []


# --- Extraction ---------------------------------------------------------------


def tf_account():
    return {"address": "azurerm_cognitive_account.oai", "type": "azurerm_cognitive_account",
            "values": {"id": ACCOUNT_ID, "name": "oai", "location": REGION,
                       "kind": "OpenAI", "sku_name": "S0"}}


def tf_deployment(name="chat", model="gpt-4o-mini", version="2024-07-18",
                  sku="GlobalStandard", account_id=ACCOUNT_ID):
    values = {"name": name, "model": [{"format": "OpenAI", "name": model,
                                       "version": version}],
              "sku": [{"name": sku, "capacity": 10}]}
    if account_id is not None:
        values["cognitive_account_id"] = account_id
    return {"address": f"azurerm_cognitive_deployment.{name}",
            "type": "azurerm_cognitive_deployment", "values": values}


def test_terraform_deployment_is_a_node_with_its_model():
    nodes = extract_resources_from_tf({"resource": [tf_account(), tf_deployment()]})
    node = nodes["azurerm_cognitive_deployment.chat"]
    assert node["service"] == "AzureOpenAI"
    assert node["nodeType"] == "compute"
    # A deployment has no location: it runs in its account's region.
    assert node["region"] == REGION
    assert node["config"] == {"model": "gpt-4o-mini", "modelVersion": "2024-07-18",
                              "deploymentType": "GlobalStandard", "account": ACCOUNT_ID}


def test_terraform_deployment_with_an_unknown_account_id_uses_the_only_account():
    # In a plan, the account ID is unknown until apply.
    nodes = extract_resources_from_tf(
        {"resource": [tf_account(), tf_deployment(account_id=None)]})
    assert nodes["azurerm_cognitive_deployment.chat"]["region"] == REGION


def test_terraform_scale_block_names_the_deployment_type():
    # azurerm provider 3.x names the SKU in a `scale` block.
    deployment = tf_deployment()
    values = deployment["values"]
    del values["sku"]
    values["scale"] = [{"type": "Standard"}]
    nodes = extract_resources_from_tf({"resource": [tf_account(), deployment]})
    assert nodes["azurerm_cognitive_deployment.chat"]["config"]["deploymentType"] == "Standard"


def test_unknown_model_warns_at_extraction():
    with pytest.warns(UserWarning, match=r"chat.*gpt-99.*no catalog rows"):
        extract_resources_from_tf({"resource": [tf_account(),
                                                tf_deployment(model="gpt-99")]})


@pytest.mark.parametrize("sku", ["ProvisionedManaged", "GlobalProvisionedManaged",
                                 "GlobalBatch"])
def test_deployment_types_without_token_prices_warn(sku):
    with pytest.warns(UserWarning, match=rf"chat.*{sku}"):
        extract_resources_from_tf({"resource": [tf_account(), tf_deployment(sku=sku)]})


def test_arm_deployments_top_level_and_nested():
    template = {"resources": [
        {"type": "Microsoft.CognitiveServices/accounts", "name": "oai", "kind": "OpenAI",
         "location": REGION, "sku": {"name": "S0"},
         "resources": [{"type": "deployments", "name": "embed",
                        "sku": {"name": "Standard", "capacity": 10},
                        "properties": {"model": {"format": "OpenAI",
                                                 "name": "text-embedding-3-small",
                                                 "version": "1"}}}]},
        {"type": "Microsoft.CognitiveServices/accounts/deployments", "name": "oai/chat",
         "sku": {"name": "GlobalStandard", "capacity": 10},
         "properties": {"model": {"format": "OpenAI", "name": "gpt-4.1",
                                  "version": "2025-04-14"}}},
    ]}
    nodes = extract_resources_from_arm(template)
    chat = nodes["Microsoft.CognitiveServices/accounts/deployments:oai/chat"]
    embed = nodes["Microsoft.CognitiveServices/accounts/deployments:oai/embed"]
    assert chat["region"] == embed["region"] == REGION
    assert chat["config"]["model"] == "gpt-4.1"
    assert chat["config"]["deploymentType"] == "GlobalStandard"
    assert embed["config"]["model"] == "text-embedding-3-small"
    assert embed["config"]["deploymentType"] == "Standard"


def test_pulumi_azure_native_deployment():
    stack = {"deployment": {"resources": [
        {"id": ACCOUNT_ID, "type": "azure-native:cognitiveservices:Account",
         "inputs": {"kind": "OpenAI", "location": REGION, "accountName": "oai"}},
        {"id": f"{ACCOUNT_ID}/deployments/chat",
         "type": "azure-native:cognitiveservices:Deployment",
         "inputs": {"accountName": "oai", "deploymentName": "chat",
                    "sku": {"name": "DataZoneStandard", "capacity": 5},
                    "properties": {"model": {"format": "OpenAI", "name": "o3-mini",
                                             "version": "2025-01-31"}}}},
    ]}}
    nodes = extract_resources_from_pulumi(stack)
    node = nodes[f"{ACCOUNT_ID}/deployments/chat"]
    assert node["region"] == REGION
    assert node["config"]["model"] == "o3-mini"
    assert node["config"]["deploymentType"] == "DataZoneStandard"


def test_pulumi_classic_deployment():
    stack = {"deployment": {"resources": [
        {"id": ACCOUNT_ID, "type": "azure:cognitive/account:Account",
         "inputs": {"kind": "OpenAI", "location": REGION, "name": "oai"}},
        {"id": f"{ACCOUNT_ID}/deployments/chat",
         "type": "azure:cognitive/deployment:Deployment",
         "inputs": {"cognitiveAccountId": ACCOUNT_ID,
                    "model": {"format": "OpenAI", "name": "gpt-4o", "version": "2024-11-20"},
                    "sku": {"name": "GlobalStandard"}}},
    ]}}
    nodes = extract_resources_from_pulumi(stack)
    node = nodes[f"{ACCOUNT_ID}/deployments/chat"]
    assert node["region"] == REGION
    assert node["config"]["modelVersion"] == "2024-11-20"


def test_deployment_resource_ids_match_the_deployment_handler_only():
    deployment_id = f"{ACCOUNT_ID}/deployments/chat"
    assert matches_arm_type(deployment_id, "Microsoft.CognitiveServices/accounts/deployments")
    assert not matches_arm_type(deployment_id, "Microsoft.CognitiveServices/accounts")
    assert not matches_arm_type(ACCOUNT_ID, "Microsoft.CognitiveServices/accounts/deployments")
    assert ResourceRegistry.from_address(deployment_id) is AzureOpenAIDeployment
    assert ResourceRegistry.from_address(ACCOUNT_ID) is AzureOpenAI


def test_extracted_deployment_is_priced(seed_catalog):
    """The extracted node keeps its config, so the engine finds its rows."""
    nodes = extract_resources_from_tf({"resource": [tf_account(), tf_deployment()]})
    address = "azurerm_cognitive_deployment.chat"
    node = {**nodes[address], "pricingModel": "token_based", "usageMetrics": TOKENS}
    model = {"version": "1.0",
             "workflow": {"name": "chat", "entry": address,
                          "frequency": {"unit": "perMonth", "value": 1000}},
             "nodes": {address: node}, "edges": []}
    costs, engine = compute(model, seed_catalog)
    # GPT-4o mini Global Standard: $0.15/M input and $0.60/M output.
    assert costs[address] == pytest.approx(0.15 + 0.30)
    assert engine.unpriced_metrics == []


def test_sync_stores_the_one_meter_per_token():
    """The descriptor keeps its meter only, and stores the price of one token."""
    from unittest.mock import MagicMock, patch

    def product(meter, usd):
        return {"productFamily": "AI + Machine Learning",
                "attributes": [{"key": "productName", "value": "Azure OpenAI"},
                               {"key": "meterName", "value": meter},
                               {"key": "meterId", "value": meter}],
                "prices": [{"USD": usd, "unit": "1K", "startUsageAmount": "0",
                            "endUsageAmount": None}]}

    catalogue = [product("gpt 4.1 nano Inp glbl Tokens", "0.0001"),
                 product("gpt 4.1 nano cached Inp glbl Tokens", "0.000025")]

    def post(url, headers=None, json=None, timeout=None):
        variables = json["variables"]
        assert variables["service"] == "Foundry Models"
        filters = variables["attributeFilters"]
        matched = [p for p in catalogue
                   if all({a["key"]: a["value"] for a in p["attributes"]}.get(f["key"])
                          == f["value"] for f in filters)]
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": {"products": matched}}
        return response

    stored = []
    cache = MagicMock()
    cache.upsert.side_effect = stored.append
    metric = "AzureOpenAI-gpt-4.1-nano-Global-Input-Token"
    with patch.object(ic.requests, "post", side_effect=post):
        assert ic.InfracostClient(api_key="test-token", org_id="org-123").sync_to_cache(
            cache, metric, REGION) == 1
    row = stored[0]
    assert (row.vendor, row.service, row.usage_metric, row.unit) == (
        "azure", "AzureOpenAI", metric, "tokens")
    assert row.price_usd == pytest.approx(0.0000001)
