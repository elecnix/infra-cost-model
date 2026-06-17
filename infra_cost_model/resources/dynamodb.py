"""DynamoDB resource model implementation."""

from typing import Optional
from .types import StorageResource, ResourceExtract


class DynamoDBTable(StorageResource):
    """DynamoDB table - storage node (leaf, no outgoing edges)."""
    
    @property
    def valid_metrics(self) -> list[str]:
        return ["readRequests", "writeRequests", "storageGb"]
    
    @classmethod
    def from_address(cls, resource_address: str) -> Optional["DynamoDBTable"]:
        """Parse resource address to determine if it's a DynamoDB table."""
        if resource_address.startswith("aws_dynamodb_table.") or \
           resource_address.startswith("aws.dynamodb.Table:") or \
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


def dynamodb_cost(read_requests: float, write_requests: float, storage_gb: float,
                  billing_mode: str = "PAY_PER_REQUEST",
                  catalog=None) -> float:
    """Calculate DynamoDB cost.
    
    Args:
        read_requests: Monthly read requests
        write_requests: Monthly write requests  
        storage_gb: Monthly storage in GB
        billing_mode: PAY_PER_REQUEST or PROVISIONED
        catalog: Optional PricingCatalog for pricing lookup
        
    Returns:
        Total monthly cost in USD.
    """
    if billing_mode == "PROVISIONED":
        return _provisioned_cost(read_requests, write_requests, storage_gb, catalog)
    
    return _on_demand_cost(read_requests, write_requests, storage_gb, catalog)


def _on_demand_cost(read_requests: float, write_requests: float, storage_gb: float,
                    catalog) -> float:
    """On-demand pricing: $1.25/M reads, $6.25/M writes, $0.25/GB storage."""
    if catalog:
        read_cost = 0.0
        write_cost = 0.0
        storage_cost = 0.0
        
        result = catalog.query("aws", "AmazonDynamoDB", "us-east-1", "Dynamo-ReadRequest", read_requests)
        if result and hasattr(result, 'total_cost'):
            read_cost = result.total_cost
            
        result = catalog.query("aws", "AmazonDynamoDB", "us-east-1", "Dynamo-WriteRequest", write_requests)
        if result and hasattr(result, 'total_cost'):
            write_cost = result.total_cost
            
        result = catalog.query("aws", "AmazonDynamoDB", "us-east-1", "Dynamo-Storage", storage_gb)
        if result and hasattr(result, 'total_cost'):
            storage_cost = result.total_cost
            
        return read_cost + write_cost + storage_cost
    
    # Fallback prices
    read_cost = read_requests * 1.25e-6  # $1.25 per million
    write_cost = write_requests * 6.25e-6  # $6.25 per million
    storage_cost = storage_gb * 0.25  # $0.25 per GB-month
    
    return read_cost + write_cost + storage_cost


def _provisioned_cost(read_requests: float, write_requests: float, storage_gb: float,
                      catalog) -> float:
    """Provisioned pricing: RCUs/WCUs at hourly rate + storage."""
    # RCUs and WCUs need to be converted to hourly average
    # Simplified: assume provisioned capacity is based on peak hourly usage
    # In reality, this would use auto-scaling settings
    
    if catalog:
        # Would query provisioned pricing metrics
        pass
    
    # Fallback: approximate hourly cost
    # $0.00013 per RCU-hour, $0.00065 per WCU-hour
    # This is a simplified calculation
    rcu_hours = read_requests / 30 / 24  # Approximate hourly RCUs
    wcu_hours = write_requests / 30 / 24  # Approximate hourly WCUs
    
    read_cost = rcu_hours * 0.00013
    write_cost = wcu_hours * 0.00065
    storage_cost = storage_gb * 0.25
    
    return read_cost + write_cost + storage_cost