"""Only OpenAI accounts are Azure OpenAI nodes (#381).

`Microsoft.CognitiveServices/accounts` and `azurerm_cognitive_account` cover
many Azure AI services, told apart by their `kind`. Only `OpenAI` and
`AIServices` (which also serves the OpenAI models) are priced as Azure
OpenAI. Any other kind, such as `SpeechServices`, is reported as unsupported.
"""
import pytest

from infra_cost_model.resources.registry import (
    extract_resources_from_arm, extract_resources_from_pulumi, extract_resources_from_tf,
)

OPENAI_KINDS = ["OpenAI", "openai", "AIServices"]
OTHER_KINDS = ["SpeechServices", "TextAnalytics", "ComputerVision", "FormRecognizer", None]


def arm_account(name, kind):
    resource = {"type": "Microsoft.CognitiveServices/accounts", "name": name,
                "location": "eastus", "sku": {"name": "S0"}}
    if kind is not None:
        resource["kind"] = kind
    return resource


@pytest.mark.parametrize("kind", OPENAI_KINDS)
def test_arm_openai_account_is_extracted(kind):
    nodes = extract_resources_from_arm({"resources": [arm_account("oai", kind)]})
    assert nodes["Microsoft.CognitiveServices/accounts:oai"]["service"] == "AzureOpenAI"


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_arm_other_account_kinds_are_unsupported(kind):
    with pytest.warns(UserWarning, match=r"could not be extracted.*accounts:speech"):
        nodes = extract_resources_from_arm({"resources": [arm_account("speech", kind)]})
    assert nodes == {}


def test_arm_kind_from_parameter_default():
    template = {
        "parameters": {"kind": {"type": "string", "defaultValue": "OpenAI"}},
        "resources": [arm_account("oai", "[parameters('kind')]")],
    }
    nodes = extract_resources_from_arm(template)
    assert list(nodes) == ["Microsoft.CognitiveServices/accounts:oai"]


def tf_account(name, kind):
    return {"address": f"azurerm_cognitive_account.{name}",
            "type": "azurerm_cognitive_account",
            "values": {"location": "eastus", "kind": kind, "sku_name": "S0"}}


@pytest.mark.parametrize("kind", OPENAI_KINDS)
def test_terraform_openai_account_is_extracted(kind):
    nodes = extract_resources_from_tf({"resource": [tf_account("oai", kind)]})
    assert nodes["azurerm_cognitive_account.oai"]["service"] == "AzureOpenAI"


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_terraform_other_account_kinds_are_unsupported(kind):
    with pytest.warns(UserWarning, match=r"could not be extracted.*azurerm_cognitive_account.lang"):
        nodes = extract_resources_from_tf({"resource": [tf_account("lang", kind)]})
    assert nodes == {}


RG = "/subscriptions/0000/resourceGroups/rg/providers"


def pulumi_account(name, kind):
    return {"id": f"{RG}/Microsoft.CognitiveServices/accounts/{name}",
            "type": "azure-native:cognitiveservices:Account",
            "inputs": {"kind": kind, "location": "eastus"}}


def test_pulumi_openai_account_is_extracted():
    stack = {"deployment": {"resources": [pulumi_account("oai", "OpenAI")]}}
    nodes = extract_resources_from_pulumi(stack)
    assert [n["service"] for n in nodes.values()] == ["AzureOpenAI"]


def test_pulumi_speech_account_is_unsupported():
    stack = {"deployment": {"resources": [pulumi_account("speech", "SpeechServices")]}}
    with pytest.warns(UserWarning, match="accounts/speech"):
        nodes = extract_resources_from_pulumi(stack)
    assert nodes == {}
