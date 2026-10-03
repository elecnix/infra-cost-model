"""Amazon RDS Instance resource model.

RDS is a core storage node with fixed hourly cost.
Pricing: instance hours vary by class (db.t3.micro $0.017/hr, Single-AZ),
Storage gp3 $0.115/GB-month, Backup $0.095/GB-month.
Multi-AZ (one standby) doubles the Single-AZ instance cost.
"""

from typing import Optional
from .types import StorageResource, ResourceExtract


class RDSInstance(StorageResource):
    """Amazon RDS Instance - storage node with fixed hourly cost."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["instanceHours", "storageGb", "backupStorageGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"instanceHours": "RDS-Instance-Hour-db.t3.micro",
                "storageGb": "RDS-Storage-gp3",
                "backupStorageGb": "RDS-Backup-Storage"}

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        """The instance class the resource settings select prices its own row."""
        instance_class = (config or {}).get("instanceClass")
        if not instance_class:
            return self.catalog_metrics
        return {**self.catalog_metrics,
                "instanceHours": f"RDS-Instance-Hour-{instance_class}"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["RDSInstance"]:
        if (resource_address.startswith("aws_db_instance.") or
                resource_address.startswith("aws.rds.Instance:") or
                resource_address.startswith("aws:rds:Instance:") or
                "RDS::DBInstance:" in resource_address):
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
