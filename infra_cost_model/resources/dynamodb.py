"""DynamoDB resource model implementation."""


from .types import StorageResource, ResourceExtract


class DynamoDBTable(StorageResource):
    """DynamoDB table - storage node (leaf, no outgoing edges)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["readRequests", "writeRequests", "storageGb",
                "readCapacityUnitHours", "writeCapacityUnitHours"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # An on-demand table bills requests. A provisioned table bills each
        # hour of each capacity unit it provisions, the units of its global
        # secondary indexes included, so a model states readCapacity x 730
        # hours a month instead of a request count (#441).
        return {"readRequests": "Dynamo-ReadRequest",
                "writeRequests": "Dynamo-WriteRequest",
                "storageGb": "Dynamo-Storage",
                "readCapacityUnitHours": "Dynamo-RCU-Hour",
                "writeCapacityUnitHours": "Dynamo-WCU-Hour"}

    @classmethod
    def from_address(cls, resource_address: str) -> StorageResource | None:
        """Parse resource address to determine if it's a DynamoDB table."""
        if resource_address.startswith("aws_dynamodb_table.") or \
           resource_address.startswith("aws.dynamodb.Table:") or \
           resource_address.startswith("aws:dynamodb:Table:") or \
           "DynamoDB::Table:" in resource_address:
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
