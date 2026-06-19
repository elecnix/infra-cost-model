"""Amazon RDS Instance resource model.

RDS is a core storage node with fixed hourly cost.
Pricing: instance hours vary by class (db.t3.micro $0.034/hr),
Storage gp3 $0.115/GB-month, Backup $0.095/GB-month.
Multi-AZ doubles instance cost.
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import StorageResource, ResourceExtract


class RDSInstance(StorageResource):
    """Amazon RDS Instance - storage node with fixed hourly cost."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["instanceHours", "storageGb", "backupStorageGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["RDSInstance"]:
        if (resource_address.startswith("aws_db_instance.") or
                resource_address.startswith("aws.rds.Instance:") or
                resource_address.startswith("aws:rds:Instance:") or
                "RDS::DBInstance" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AmazonRDS",
            region=values.get("region"),
            config={
                "identifier": values.get("identifier"),
                "engine": values.get("engine"),
                "instanceClass": values.get("instance_class"),
                "allocatedStorage": values.get("allocated_storage"),
                "storageType": values.get("storage_type", "gp3"),
                "multiAz": values.get("multi_az", False),
                "backupRetentionPeriod": values.get("backup_retention_period", 7),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AmazonRDS",
            region=inputs.get("region"),
            config={
                "identifier": inputs.get("identifier"),
                "engine": inputs.get("engine"),
                "instanceClass": inputs.get("instanceClass"),
                "allocatedStorage": inputs.get("allocatedStorage"),
                "storageType": inputs.get("storageType", "gp3"),
                "multiAz": inputs.get("multiAz", False),
                "backupRetentionPeriod": inputs.get("backupRetentionPeriod", 7),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AmazonRDS",
            region=None,
            config={
                "identifier": properties.get("DBInstanceIdentifier"),
                "engine": properties.get("Engine"),
                "instanceClass": properties.get("DBInstanceClass"),
                "allocatedStorage": properties.get("AllocatedStorage"),
                "storageType": properties.get("StorageType", "gp3"),
                "multiAz": properties.get("MultiAZ", False),
                "backupRetentionPeriod": properties.get("BackupRetentionPeriod", 7),
            },
        )


def _rds_cost(instance_hours=730, instance_class="db.t3.micro", storage_gb=20,
              backup_storage_gb=0, multi_az=False, catalog=None, provider: str = "aws", region="us-east-1") -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    metric = f"RDS-Instance-Hour-{instance_class}"
    r = catalog.query(provider, "AmazonRDS", region, metric, instance_hours)
    if r and hasattr(r, "total_cost"):
        instance_cost = r.total_cost
        if multi_az:
            mult_result = catalog.query("aws", "AmazonRDS", region, "RDS-Multi-AZ-Multiplier")
            multi_az_rate = 2.0
            if mult_result is not None and hasattr(mult_result, "price_usd"):
                multi_az_rate = mult_result.price_usd
            instance_cost *= multi_az_rate
        total += instance_cost
    if storage_gb > 0:
        r = catalog.query(provider, "AmazonRDS", region, "RDS-Storage-gp3", storage_gb)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    if backup_storage_gb > 0:
        r = catalog.query(provider, "AmazonRDS", region, "RDS-Backup-Storage", backup_storage_gb)
        if r and hasattr(r, "total_cost"): total += r.total_cost
    return total
