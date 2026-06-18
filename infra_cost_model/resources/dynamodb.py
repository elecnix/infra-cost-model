"""DynamoDB resource model implementation."""

from infra_cost_model.pricing.catalog import PricingCatalog

from .types import StorageResource, ResourceExtract


class DynamoDBTable(StorageResource):
    """DynamoDB table - storage node (leaf, no outgoing edges)."""
    
    @property
    def valid_metrics(self) -> list[str]:
        return ["readRequests", "writeRequests", "storageGb"]
    
    @classmethod
    def from_address(cls, resource_address: str) -> StorageResource | None:
        """Parse resource address to determine if it's a DynamoDB table."""
        if resource_address.startswith("aws_dynamodb_table.") or \
           resource_address.startswith("aws.dynamodb.Table:") or \
           resource_address.startswith("aws:dynamodb:Table:") or \
           "DynamoDB::Table" in resource_address:
            return cls()
        return None
    
    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """Extract from Terraform aws_dynamodb_table resource."""
        values = resource.get("values", {})
        
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage",
            provider="aws",
            service="AmazonDynamoDB",
            region=values.get("region"),
            config={
                "billingMode": values.get("billing_mode"),
                "hashKey": values.get("hash_key"),
                "rangeKey": values.get("range_key"),
                "readCapacity": values.get("read_capacity"),
                "writeCapacity": values.get("write_capacity"),
            }
        )
    
    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        """Extract from Pulumi aws.dynamodb.Table resource."""
        inputs = resource.get("inputs", {})
        
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage",
            provider="aws",
            service="AmazonDynamoDB",
            region=inputs.get("region"),
            config={
                "billingMode": inputs.get("billingMode"),
                "hashKey": inputs.get("hashKey"),
                "rangeKey": inputs.get("rangeKey"),
                "readCapacity": inputs.get("readCapacity"),
                "writeCapacity": inputs.get("writeCapacity"),
            }
        )
    
    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        """Extract from CDK CloudFormation DynamoDB::Table."""
        properties = resource.get("Properties", {})
        key_schema = properties.get("KeySchema", [])
        billing_mode = properties.get("BillingMode", "PAY_PER_REQUEST")
        
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage",
            provider="aws",
            service="AmazonDynamoDB",
            region=None,
            config={
                "billingMode": billing_mode,
                "hashKey": key_schema[0].get("AttributeName") if key_schema else None,
                "rangeKey": key_schema[1].get("AttributeName") if len(key_schema) > 1 else None,
                "readCapacity": properties.get("ProvisionedThroughput", {}).get("ReadCapacityUnits"),
                "writeCapacity": properties.get("ProvisionedThroughput", {}).get("WriteCapacityUnits"),
            }
        )


def _dynamodb_cost(read_requests: float, write_requests: float, storage_gb: float,
                   billing_mode: str = "PAY_PER_REQUEST",
                   catalog=None, gsi_read_requests: float = 0,
                   gsi_write_requests: float = 0,
                   region: str = "us-east-1") -> float:
    """Calculate DynamoDB cost.
    
    Args:
        read_requests: Monthly read requests
        write_requests: Monthly write requests  
        storage_gb: Storage in GB-month
        billing_mode: "PAY_PER_REQUEST" or "PROVISIONED"
        catalog: Optional PricingCatalog (uses default if None, auto-loads seed)
        gsi_read_requests: GSI read request units
        gsi_write_requests: GSI write request units
        region: AWS region
        
    Returns:
        Total monthly cost in USD.
    """
    read_requests += gsi_read_requests
    write_requests += gsi_write_requests
    
    if catalog is None:
        catalog = PricingCatalog()
    
    if billing_mode == "PROVISIONED":
        return _provisioned_cost(
            read_requests, write_requests, storage_gb, catalog,
            region
        )
    return _on_demand_cost(
        read_requests, write_requests, storage_gb, catalog,
        region
    )


def _on_demand_cost(read_requests: float, write_requests: float, storage_gb: float,
                    catalog=None, region: str = "us-east-1") -> float:
    """On-demand pricing using catalog prices."""
    if catalog is None:
        catalog = PricingCatalog()
    
    read_cost = catalog.query("aws", "AmazonDynamoDB", region, "Dynamo-ReadRequest", read_requests)
    write_cost = catalog.query("aws", "AmazonDynamoDB", region, "Dynamo-WriteRequest", write_requests)
    storage_cost = catalog.query("aws", "AmazonDynamoDB", region, "Dynamo-Storage", storage_gb)

    total = 0.0
    for result in [read_cost, write_cost, storage_cost]:
        if result and hasattr(result, 'total_cost'):
            total += result.total_cost
    return total


def _provisioned_cost(rcu_hours: float, wcu_hours: float, storage_gb: float,
                      catalog=None, region: str = "us-east-1") -> float:
    """Provisioned pricing using catalog prices."""
    if catalog is None:
        catalog = PricingCatalog()
    
    rcu_cost = catalog.query("aws", "AmazonDynamoDB", region, "Dynamo-RCU-Hour", rcu_hours)
    wcu_cost = catalog.query("aws", "AmazonDynamoDB", region, "Dynamo-WCU-Hour", wcu_hours)
    storage_cost = catalog.query("aws", "AmazonDynamoDB", region, "Dynamo-Storage", storage_gb)

    total = 0.0
    for result in [rcu_cost, wcu_cost, storage_cost]:
        if result and hasattr(result, 'total_cost'):
            total += result.total_cost
    return total


def _provisioned_dynamodb_cost(rcu_hours: float, wcu_hours: float, storage_gb: float,
                               catalog=None,
                               gsi_rcu_hours: float = 0, gsi_wcu_hours: float = 0,
                               region: str = "us-east-1") -> float:
    """Calculate DynamoDB provisioned costs from RCU/WCU hours."""
    total_rcu = rcu_hours + gsi_rcu_hours
    total_wcu = wcu_hours + gsi_wcu_hours
    return _provisioned_cost(
        total_rcu, total_wcu, storage_gb, catalog, region
    )
