"""Amazon RDS Instance resource model.

RDS is a core storage node with fixed hourly cost. The ``engine`` and
``multiAz`` settings select the rows that price it (#481):

- MySQL and MariaDB bill the same instance rates (db.t3.micro $0.017 an
  hour, Single-AZ), so both use the ``RDS-Instance-Hour-<class>`` rows. An
  absent engine uses them too.
- PostgreSQL has its own rows, ``RDS-Instance-Hour-postgres-<class>``
  (db.t3.micro $0.018 an hour).
- Any other engine (Oracle, SQL Server, Db2, Aurora) names a row that no
  catalog prices, so the engine reports its instance-hours as unpriced, and
  extraction warns about it.
- Multi-AZ (one standby) bills twice the Single-AZ instance and gp3 storage
  rates in each engine, so the handler maps those rows with a factor of 2.

Storage gp3 is $0.115 a GB-month and backup $0.095 a GB-month for each of
the three priced engines. Prices from https://aws.amazon.com/rds/mysql/pricing/
and https://aws.amazon.com/rds/postgresql/pricing/, us-east-1, on demand.
"""

import warnings
from typing import Any, Optional
from .types import StorageResource, ResourceExtract

# Engines billed at the MySQL instance rates. The Cloud Pricing API gave the
# same MariaDB and MySQL rates for each seed class on 2026-10-08.
_MYSQL_RATE_ENGINES = frozenset({"mysql", "mariadb"})
_POSTGRES_ENGINE = "postgres"
# One standby doubles the instance and its storage (AWS RDS pricing pages).
MULTI_AZ_FACTOR = 2


def _engine(config: dict) -> Optional[str]:
    engine = config.get("engine")
    return None if engine is None else str(engine).lower()


def _multi_az(config: dict) -> bool:
    value = config.get("multiAz")
    # CloudFormation templates can state the boolean as a string.
    return value is True or str(value).lower() == "true"


def engine_is_priced(engine: Any) -> bool:
    """Whether catalog rows price the instance-hours of *engine*."""
    if engine is None:
        return True
    return str(engine).lower() in _MYSQL_RATE_ENGINES | {_POSTGRES_ENGINE}


def instance_hour_metric(engine: Optional[str], instance_class: str) -> str:
    """The catalog row of an instance of *engine* and *instance_class*."""
    if engine is None or engine in _MYSQL_RATE_ENGINES:
        return f"RDS-Instance-Hour-{instance_class}"
    if engine == _POSTGRES_ENGINE:
        return f"RDS-Instance-Hour-postgres-{instance_class}"
    # Not under the "RDS-Instance-Hour-" prefix, so no descriptor prices it
    # at the MySQL rate either.
    return f"RDS-{engine}-Instance-Hour-{instance_class}"


def _warn_unpriced_engine(address: str, engine: Any) -> None:
    if not engine_is_priced(engine):
        warnings.warn(
            f"{address}: RDS engine {engine!r} has no catalog rows, so the engine "
            f"reports its instance-hours as unpriced."
        )


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

    def catalog_metrics_for(self, config: dict) -> dict:
        """The engine, instance class and deployment select the rows."""
        config = config or {}
        metrics: dict = dict(self.catalog_metrics)
        instance_class = config.get("instanceClass")
        # Only an absent class falls back to the default class. An empty
        # string is a class the input declared and no row prices, so it
        # selects its own row and is reported unpriced rather than silently
        # billed at the db.t3.micro rate.
        if instance_class is None:
            instance_class = "db.t3.micro"
        metrics["instanceHours"] = instance_hour_metric(_engine(config), instance_class)
        if _multi_az(config):
            for logical in ("instanceHours", "storageGb"):
                metrics[logical] = {metrics[logical]: MULTI_AZ_FACTOR}
        return metrics

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
        _warn_unpriced_engine(resource.get("address", ""), values.get("engine"))
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
        _warn_unpriced_engine(resource.get("id", ""), inputs.get("engine"))
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
        _warn_unpriced_engine(resource.get("LogicalId", ""), properties.get("Engine"))
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
