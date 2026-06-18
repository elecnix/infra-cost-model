"""Tests for multi-cloud provider dispatch and GCP/Azure resource handlers (DP#6)."""

import pytest
from infra_cost_model.resources.registry import ResourceRegistry
from infra_cost_model.resources.gcp import CloudFunction, CloudStorage, CloudRun, Firestore
from infra_cost_model.resources.azure import (
    AzureFunction, CosmosDB, APIManagement, AzureOpenAI, AzureBlobStorage,
)


class TestProviderDispatch:
    """Provider-based handler lookup in ResourceRegistry."""

    def test_supported_providers_includes_aws_gcp_azure(self):
        providers = ResourceRegistry.supported_providers()
        assert "aws" in providers
        assert "gcp" in providers
        assert "azure" in providers

    def test_handlers_by_provider_gcp(self):
        handlers = ResourceRegistry.handlers_by_provider("gcp")
        handler_names = {h.__name__ for h in handlers}
        assert "CloudFunction" in handler_names
        assert "CloudStorage" in handler_names
        assert "CloudRun" in handler_names
        assert "Firestore" in handler_names

    def test_handlers_by_provider_azure(self):
        handlers = ResourceRegistry.handlers_by_provider("azure")
        handler_names = {h.__name__ for h in handlers}
        assert "AzureFunction" in handler_names
        assert "CosmosDB" in handler_names
        assert "APIManagement" in handler_names
        assert "AzureOpenAI" in handler_names
        assert "AzureBlobStorage" in handler_names

    def test_handlers_by_provider_aws(self):
        handlers = ResourceRegistry.handlers_by_provider("aws")
        handler_names = {h.__name__ for h in handlers}
        assert "LambdaFunction" in handler_names
        assert "DynamoDBTable" in handler_names
        assert "APIGatewayHTTP" in handler_names

    def test_provider_qualified_lookup_gcp(self):
        handler = ResourceRegistry.from_address(
            "google_cloudfunctions_function.my_func", provider="gcp"
        )
        assert handler is not None
        assert handler == CloudFunction

    def test_provider_qualified_lookup_azure(self):
        handler = ResourceRegistry.from_address(
            "azurerm_function_app.my_func", provider="azure"
        )
        assert handler is not None
        assert handler == AzureFunction

    def test_unqualified_lookup_still_works(self):
        """Without provider hint, all handlers are searched."""
        handler = ResourceRegistry.from_address("aws_lambda_function.test")
        assert handler is not None


class TestGCPHandlers:
    """GCP resource handler address matching and extraction."""

    def test_cloud_function_terraform(self):
        result = CloudFunction.from_address("google_cloudfunctions_function.my_func")
        assert result is not None
        assert result.node_type == "compute"

    def test_cloud_function_pulumi(self):
        result = CloudFunction.from_address("google:cloudfunctions:Function:my-func")
        assert result is not None

    def test_cloud_function_cdk(self):
        result = CloudFunction.from_address("MyStack/Func/CloudFunctions::Function")
        assert result is not None

    def test_cloud_storage_terraform(self):
        result = CloudStorage.from_address("google_storage_bucket.my_bucket")
        assert result is not None
        assert result.node_type == "storage"

    def test_cloud_run_terraform(self):
        result = CloudRun.from_address("google_cloud_run_service.my_svc")
        assert result is not None
        assert result.node_type == "routing"

    def test_firestore_terraform(self):
        result = Firestore.from_address("google_firestore_database.my_db")
        assert result is not None
        assert result.node_type == "storage"

    def test_gcp_extract_tf_cloud_function(self):
        resource = {
            "address": "google_cloudfunctions_function.hello",
            "type": "google_cloudfunctions_function",
            "values": {
                "available_memory_mb": 256,
                "timeout": 60,
                "runtime": "python311",
                "region": "us-central1",
            },
        }
        result = CloudFunction.extract_tf(resource)
        assert result.provider == "gcp"
        assert result.service == "CloudFunctions"
        assert result.config["memoryMb"] == 256
        assert result.config["runtime"] == "python311"


class TestAzureHandlers:
    """Azure resource handler address matching and extraction."""

    def test_azure_function_terraform(self):
        result = AzureFunction.from_address("azurerm_function_app.my_func")
        assert result is not None
        assert result.node_type == "compute"

    def test_azure_function_linux_terraform(self):
        result = AzureFunction.from_address("azurerm_linux_function_app.my_func")
        assert result is not None

    def test_azure_function_cdk(self):
        result = AzureFunction.from_address("sites/Microsoft.Web/sites")
        assert result is not None

    def test_cosmosdb_terraform(self):
        result = CosmosDB.from_address("azurerm_cosmosdb_account.my_db")
        assert result is not None
        assert result.node_type == "storage"

    def test_api_management_terraform(self):
        result = APIManagement.from_address("azurerm_api_management.my_apim")
        assert result is not None
        assert result.node_type == "routing"

    def test_azure_openai_terraform(self):
        result = AzureOpenAI.from_address("azurerm_cognitive_account.my_oai")
        assert result is not None
        assert result.node_type == "compute"

    def test_azure_blob_storage_terraform(self):
        result = AzureBlobStorage.from_address("azurerm_storage_account.my_sa")
        assert result is not None
        assert result.node_type == "storage"

    def test_azure_extract_tf_function(self):
        resource = {
            "address": "azurerm_function_app.my_func",
            "type": "azurerm_function_app",
            "values": {
                "location": "eastus",
                "service_plan_id": "plan123",
                "app_settings": {"FUNCTIONS_WORKER_RUNTIME": "python"},
            },
        }
        result = AzureFunction.extract_tf(resource)
        assert result.provider == "azure"
        assert result.service == "AzureFunctions"
        assert result.region == "eastus"

    def test_azure_unknown_address_returns_none(self):
        result = AzureFunction.from_address("aws_lambda_function.test")
        assert result is None


class TestRegistryMultiCloudExtract:
    """End-to-end extraction across cloud providers."""

    def test_extract_gcp_resource(self):
        resource = {
            "address": "google_cloudfunctions_function.hello",
            "type": "google_cloudfunctions_function",
            "values": {
                "available_memory_mb": 256,
                "timeout": 60,
                "runtime": "python311",
                "region": "us-central1",
            },
        }
        result = ResourceRegistry.extract(
            "google_cloudfunctions_function.hello", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "gcp"
        assert result["service"] == "CloudFunctions"

    def test_extract_azure_resource(self):
        resource = {
            "address": "azurerm_cosmosdb_account.mydb",
            "type": "azurerm_cosmosdb_account",
            "values": {
                "location": "westeurope",
                "offer_type": "Standard",
                "kind": "GlobalDocumentDB",
            },
        }
        result = ResourceRegistry.extract(
            "azurerm_cosmosdb_account.mydb", resource, "terraform"
        )
        assert result is not None
        assert result["provider"] == "azure"
        assert result["service"] == "CosmosDB"
