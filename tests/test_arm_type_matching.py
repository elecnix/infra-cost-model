"""ARM resource-type matching precision tests (#222).

`extract_resources_from_arm` builds addresses as ``f"{type}:{name}"`` (for
example ``Microsoft.Web/sites:func-orders``). A child resource has a longer
type (``Microsoft.Web/sites/slots:func-orders/staging``), so a loose
``"Microsoft.Web/sites" in addr`` test would price a deployment slot as a
Function App. Each handler must require its type to be followed by ``:``, the
same fix #216 made for CloudFormation types.

Azure resource IDs (``/subscriptions/.../providers/Microsoft.Web/sites/x``)
reach the same handlers through Pulumi's ``id`` field. For those, the parent
type must be followed by exactly one name segment.
"""
import pytest

from infra_cost_model.resources.azure import (
    APIManagement, AzureBlobStorage, AzureFunction, AzureOpenAI, CosmosDB,
)
from infra_cost_model.resources.registry import ResourceRegistry

RG = "/subscriptions/0000/resourceGroups/rg/providers"

# (handler, matching ARM address, child address that must be excluded)
CASES = [
    (AzureFunction, "Microsoft.Web/sites:func-orders",
     "Microsoft.Web/sites/slots:func-orders/staging"),
    (AzureBlobStorage, "Microsoft.Storage/storageAccounts:storders",
     "Microsoft.Storage/storageAccounts/blobServices:storders/default"),
    (CosmosDB, "Microsoft.DocumentDB/databaseAccounts:cosmos-orders",
     "Microsoft.DocumentDB/databaseAccounts/sqlDatabases:cosmos-orders/orders"),
    (APIManagement, "Microsoft.ApiManagement/service:apim-orders",
     "Microsoft.ApiManagement/service/apis:apim-orders/orders"),
    (AzureOpenAI, "Microsoft.CognitiveServices/accounts:oai-orders",
     "Microsoft.CognitiveServices/accounts/deployments:oai-orders/gpt-4o"),
]

# The same pairs in Azure resource ID form.
ID_CASES = [
    (AzureFunction, f"{RG}/Microsoft.Web/sites/func-orders",
     f"{RG}/Microsoft.Web/sites/func-orders/slots/staging"),
    (AzureBlobStorage, f"{RG}/Microsoft.Storage/storageAccounts/storders",
     f"{RG}/Microsoft.Storage/storageAccounts/storders/blobServices/default"),
    (CosmosDB, f"{RG}/Microsoft.DocumentDB/databaseAccounts/cosmos-orders",
     f"{RG}/Microsoft.DocumentDB/databaseAccounts/cosmos-orders/sqlDatabases/orders"),
    (APIManagement, f"{RG}/Microsoft.ApiManagement/service/apim-orders",
     f"{RG}/Microsoft.ApiManagement/service/apim-orders/apis/orders"),
    (AzureOpenAI, f"{RG}/Microsoft.CognitiveServices/accounts/oai-orders",
     f"{RG}/Microsoft.CognitiveServices/accounts/oai-orders/deployments/gpt-4o"),
]


@pytest.mark.parametrize("handler,match_addr,child_addr", CASES,
                         ids=[c[0].__name__ for c in CASES])
def test_arm_type_matches_parent_not_child(handler, match_addr, child_addr):
    assert handler.from_address(match_addr) is not None, (
        f"{handler.__name__} should match its own ARM address {match_addr!r}")
    assert handler.from_address(child_addr) is None, (
        f"{handler.__name__} must NOT match child type {child_addr!r}")


@pytest.mark.parametrize("handler,match_id,child_id", ID_CASES,
                         ids=[c[0].__name__ for c in ID_CASES])
def test_resource_id_matches_parent_not_child(handler, match_id, child_id):
    assert handler.from_address(match_id) is not None
    assert handler.from_address(child_id) is None


def test_arm_type_match_ignores_case():
    """ARM resource types are case-insensitive."""
    assert AzureFunction.from_address("microsoft.web/Sites:func-orders") is not None


def test_prefix_sibling_type_is_excluded():
    """A type that only starts with the parent's name is a different type."""
    assert AzureFunction.from_address("Microsoft.Web/sitesExtra:x") is None


def test_registry_dispatches_site_not_slot():
    assert ResourceRegistry.from_address("Microsoft.Web/sites:func-orders") is AzureFunction
    assert ResourceRegistry.from_address(
        "Microsoft.Web/sites/slots:func-orders/staging") is None
