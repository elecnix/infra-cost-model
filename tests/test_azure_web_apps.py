"""Web apps are not Function Apps (#364).

The ARM type `Microsoft.Web/sites` covers App Service web apps (`kind: app`)
and Function Apps (`kind: functionapp`). Only a site whose `kind` names
`functionapp` is priced as a Function App. Terraform names the two with
separate resource types.
"""
import pytest

from infra_cost_model.resources.azure import AzureFunction
from infra_cost_model.resources.registry import (
    ResourceRegistry, extract_resources_from_arm, extract_resources_from_pulumi,
)


def site(name, kind):
    resource = {"type": "Microsoft.Web/sites", "name": name, "location": "eastus"}
    if kind is not None:
        resource["kind"] = kind
    return resource


@pytest.mark.parametrize("kind", ["functionapp", "functionapp,linux",
                                  "FunctionApp,Linux,Container", "functionapp,workflowapp"])
def test_arm_function_app_is_extracted(kind):
    nodes = extract_resources_from_arm({"resources": [site("func", kind)]})
    assert nodes["Microsoft.Web/sites:func"]["service"] == "AzureFunctions"


@pytest.mark.parametrize("kind", ["app", "app,linux", "app,linux,container", None])
def test_arm_web_app_is_unsupported(kind):
    with pytest.warns(UserWarning, match=r"could not be extracted.*Microsoft.Web/sites:web"):
        nodes = extract_resources_from_arm({"resources": [site("web", kind)]})
    assert nodes == {}


def test_arm_template_with_both_kinds():
    template = {"resources": [site("func", "functionapp"), site("web", "app")]}
    with pytest.warns(UserWarning, match="Microsoft.Web/sites:web"):
        nodes = extract_resources_from_arm(template)
    assert list(nodes) == ["Microsoft.Web/sites:func"]


@pytest.mark.parametrize("address", [
    "azurerm_function_app.f",
    "azurerm_linux_function_app.f",
    "azurerm_windows_function_app.f",
])
def test_terraform_function_app_types_match(address):
    assert ResourceRegistry.from_address(address) is AzureFunction


@pytest.mark.parametrize("address", [
    "azurerm_linux_web_app.w",
    "azurerm_windows_web_app.w",
    "azurerm_app_service.w",
])
def test_terraform_web_app_types_do_not_match(address):
    assert AzureFunction.from_address(address) is None


RG = "/subscriptions/0000/resourceGroups/rg/providers"


def pulumi_site(name, kind):
    return {
        "id": f"{RG}/Microsoft.Web/sites/{name}",
        "type": "azure-native:web:WebApp",
        "inputs": {"kind": kind, "location": "eastus"},
    }


def test_pulumi_function_app_by_resource_id_is_extracted():
    stack = {"deployment": {"resources": [pulumi_site("func", "functionapp")]}}
    nodes = extract_resources_from_pulumi(stack)
    assert [n["service"] for n in nodes.values()] == ["AzureFunctions"]


def test_pulumi_web_app_by_resource_id_is_unsupported():
    stack = {"deployment": {"resources": [pulumi_site("web", "app,linux")]}}
    with pytest.warns(UserWarning, match="Microsoft.Web/sites/web"):
        nodes = extract_resources_from_pulumi(stack)
    assert nodes == {}
