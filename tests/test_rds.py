"""Tests for Amazon RDS Instance resource model (Issue #19)."""
import pytest
from infra_cost_model.resources.rds import RDSInstance
from infra_cost_model.pricing.catalog import PricingCatalog
from live_pricing import resource_cost

class TestRDSAddressParsing:
    def test_from_address_terraform(self):
        r = RDSInstance.from_address("aws_db_instance.main")
        assert r is not None and r.node_type == "storage"
    def test_from_address_pulumi(self):
        r = RDSInstance.from_address("aws.rds.Instance:main-db")
        assert r is not None and r.node_type == "storage"
    def test_from_address_cdk(self):
        r = RDSInstance.from_address("AWS::RDS::DBInstance:MainDB")
        assert r is not None and r.node_type == "storage"
    def test_from_address_aws_format(self):
        assert RDSInstance.from_address("aws:rds:Instance:prod-db") is not None
    def test_from_address_unrelated(self):
        assert RDSInstance.from_address("aws_lambda_function.handler") is None

class TestRDSExtraction:
    def test_extract_tf(self):
        resource = {"address": "aws_db_instance.main", "type": "aws_db_instance", "values": {"identifier": "main-database", "engine": "postgres", "instance_class": "db.t3.micro", "allocated_storage": 20, "storage_type": "gp3", "multi_az": False, "region": "us-east-1", "backup_retention_period": 7, "publicly_accessible": False, "deletion_protection": True}}
        result = RDSInstance.extract_tf(resource)
        assert result.node_type == "storage" and result.provider == "aws" and result.service == "AmazonRDS"
        assert result.config["identifier"] == "main-database" and result.config["engine"] == "postgres"
        assert result.config["instanceClass"] == "db.t3.micro" and result.config["allocatedStorage"] == 20
        assert result.config["storageType"] == "gp3" and result.config["multiAz"] is False
    def test_extract_tf_multi_az(self):
        resource = {"address": "aws_db_instance.prod", "type": "aws_db_instance", "values": {"identifier": "prod-db", "engine": "mysql", "instance_class": "db.m5.large", "allocated_storage": 100, "multi_az": True, "region": "us-east-1"}}
        assert RDSInstance.extract_tf(resource).config["multiAz"] is True
    def test_extract_pulumi(self):
        resource = {"id": "aws.rds.Instance:app-db", "type": "aws.rds.Instance", "inputs": {"identifier": "app-database", "engine": "postgres", "instanceClass": "db.t3.small", "allocatedStorage": 50, "storageType": "gp3", "multiAz": True, "region": "us-west-2"}}
        result = RDSInstance.extract_pulumi(resource)
        assert result.provider == "aws" and result.config["instanceClass"] == "db.t3.small" and result.config["multiAz"] is True
    def test_extract_cdk(self):
        resource = {"Type": "AWS::RDS::DBInstance", "LogicalId": "MainDatabase", "Properties": {"DBInstanceIdentifier": "main-db", "Engine": "postgres", "DBInstanceClass": "db.t3.micro", "AllocatedStorage": "20", "StorageType": "gp3", "MultiAZ": False, "BackupRetentionPeriod": 30, "PubliclyAccessible": False}}
        result = RDSInstance.extract_cdk(resource)
        assert result.config["engine"] == "postgres" and result.config["instanceClass"] == "db.t3.micro"

class TestRDSPricing:
    def setup_method(self):
        self.catalog = PricingCatalog(seed=True)

        def cost(instance_hours=0, storage_gb=0, backup_storage_gb=0,
                 instance_class=None):
            config = {"instanceClass": instance_class} if instance_class else None
            return resource_cost("aws_db_instance.db", "AmazonRDS", "us-east-1",
                                 catalog=self.catalog, config=config,
                                 instanceHours=instance_hours,
                                 storageGb=storage_gb,
                                 backupStorageGb=backup_storage_gb)

        self.cost = cost

    def test_fixed_cost_t3_micro(self):
        cost = self.cost(instance_hours=730, instance_class="db.t3.micro")
        assert cost == pytest.approx(12.41, rel=0.01)

    def test_fixed_cost_t3_small(self):
        cost = self.cost(instance_hours=730, instance_class="db.t3.small")
        assert cost == pytest.approx(24.82, rel=0.01)

    def test_fixed_cost_m5_large(self):
        cost = self.cost(instance_hours=730, instance_class="db.m5.large")
        assert cost == pytest.approx(124.83, rel=0.01)

    def test_storage_cost_gp3(self):
        cost = self.cost(storage_gb=100)
        assert cost == pytest.approx(11.50, rel=0.01)

    def test_combined_instance_and_storage(self):
        cost = self.cost(instance_hours=730, instance_class="db.t3.micro", storage_gb=20)
        assert cost == pytest.approx(14.71, rel=0.01)

    def test_backup_storage_beyond_free_tier(self):
        cost = self.cost(backup_storage_gb=50)
        assert cost == pytest.approx(4.75, rel=0.01)

    def test_zero_usage(self):
        assert self.cost() == 0.0


    def test_an_absent_instance_class_keeps_the_default_row(self):
        assert (RDSInstance().catalog_metrics_for({})["instanceHours"]
                == "RDS-Instance-Hour-db.t3.micro")

    def test_an_empty_instance_class_selects_its_own_row(self):
        """An empty class is a class the input declared, not an absent one.

        Falling back to the default map would bill it at db.t3.micro, which is
        the silent mispricing #427 removed. The row it selects prices nothing,
        so the engine reports it unpriced.
        """
        assert (RDSInstance().catalog_metrics_for({"instanceClass": ""})["instanceHours"]
                == "RDS-Instance-Hour-")

    def test_an_empty_instance_class_has_no_priced_row(self):
        """The row an empty class selects is unpriced, not the micro rate."""
        assert self.catalog.query("aws", "AmazonRDS", "us-east-1",
                                  "RDS-Instance-Hour-db.t3.micro")
        assert not self.catalog.query("aws", "AmazonRDS", "us-east-1",
                                      "RDS-Instance-Hour-")

class TestRDSLeafNode:
    def test_rds_is_storage_leaf_node(self):
        result = RDSInstance.from_address("aws_db_instance.test")
        assert result is not None and result.node_type == "storage"
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node("storage") is True and is_leaf_node(result.node_type) is True
    def test_rds_valid_metrics(self):
        i = RDSInstance()
        assert all(m in i.valid_metrics for m in ["instanceHours", "storageGb", "backupStorageGb"])

class TestRDSRegistryIntegration:
    def test_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_db_instance.main") == RDSInstance
    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {"address": "aws_db_instance.main", "type": "aws_db_instance", "values": {"identifier": "main-db", "engine": "postgres", "instance_class": "db.t3.micro", "allocated_storage": 20, "region": "us-east-1"}}
        result = ResourceRegistry.extract("aws_db_instance.main", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonRDS" and result["nodeType"] == "storage"


def test_a_regional_metric_missing_from_its_region_is_not_priced_from_us_east_1():
    """The helper must fall back only for a global metric, as the engine does.

    RDS is regional and the seed carries no eu-west-1 RDS rows. Before the
    `is_global_metric` gate this helper walked GLOBAL_PRICE_REGIONS anyway and
    priced the node from us-east-1, so a test could assert a non-zero total for
    a node the engine leaves unpriced. Now it refuses, which is the whole point
    of a helper that claims to resolve "the way the engine does".
    """
    catalog = PricingCatalog(seed=True)
    with pytest.raises(AssertionError, match="no catalog rows"):
        resource_cost("aws_db_instance.db", "AmazonRDS", "eu-west-1",
                      catalog=catalog, instanceHours=100)


# --- Engine and deployment (#481) ----------------------------------------------
#
# Prices from the Infracost Cloud Pricing API on 2026-10-08, us-east-1, on
# demand, matching https://aws.amazon.com/rds/mysql/pricing/ and
# https://aws.amazon.com/rds/postgresql/pricing/. Multi-AZ (one standby) is
# twice Single-AZ for the instance and for gp3 storage, in each engine.

ADDRESS = "aws_db_instance.db"


def _model(config, hours=730, storage_gb=20, backup_gb=0):
    usage = {"instanceHours": {"unit": "hours", "value": hours, "fixed": True}}
    if storage_gb:
        usage["storageGb"] = {"unit": "GB-Mo", "value": storage_gb, "fixed": True}
    if backup_gb:
        usage["backupStorageGb"] = {"unit": "GB-Mo", "value": backup_gb, "fixed": True}
    node = {"nodeType": "storage", "resourceAddress": ADDRESS, "provider": "aws",
            "service": "AmazonRDS", "region": "us-east-1", "usageMetrics": usage,
            "config": config}
    return {"version": "1.0",
            "workflow": {"name": "w", "entry": ADDRESS,
                         "frequency": {"unit": "perMonth", "value": 1}},
            "nodes": {ADDRESS: node}, "edges": []}


def _compute(config, catalog, **kwargs):
    import warnings
    from infra_cost_model.engine.engine import CostEngine
    engine = CostEngine(_model(config, **kwargs), catalog=catalog, time_basis="monthly")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        costs = engine.compute()
    return costs[ADDRESS], engine


@pytest.mark.parametrize("engine_name", [None, "mysql", "MySQL", "mariadb"])
@pytest.mark.parametrize("instance_class,hourly", [
    ("db.t3.micro", 0.017), ("db.t3.small", 0.034), ("db.m5.large", 0.171)])
def test_mysql_and_mariadb_keep_the_mysql_rows(seed_catalog, engine_name,
                                               instance_class, hourly):
    """MariaDB and MySQL bill the same rate for each seed class."""
    config = {"instanceClass": instance_class, "engine": engine_name}
    assert (RDSInstance().catalog_metrics_for(config)["instanceHours"]
            == f"RDS-Instance-Hour-{instance_class}")
    cost, engine = _compute(config, seed_catalog)
    assert cost == pytest.approx(730 * hourly + 20 * 0.115)
    assert engine.unpriced_metrics == []


@pytest.mark.parametrize("instance_class,hourly", [
    ("db.t3.micro", 0.018), ("db.t3.small", 0.036), ("db.m5.large", 0.178)])
def test_postgres_selects_and_prices_its_own_rows(seed_catalog, instance_class, hourly):
    config = {"instanceClass": instance_class, "engine": "postgres"}
    assert (RDSInstance().catalog_metrics_for(config)["instanceHours"]
            == f"RDS-Instance-Hour-postgres-{instance_class}")
    cost, engine = _compute(config, seed_catalog)
    assert cost == pytest.approx(730 * hourly + 20 * 0.115)
    assert engine.unpriced_metrics == []


@pytest.mark.parametrize("engine_name,hourly", [("mysql", 0.017), ("postgres", 0.018)])
def test_multi_az_doubles_the_instance_and_storage_cost(seed_catalog, engine_name, hourly):
    """One standby: $0.034 (MySQL) and $0.036 (PostgreSQL) an hour for a
    db.t3.micro, and $0.23 a GB-month of gp3. Backup storage is not doubled."""
    single = {"instanceClass": "db.t3.micro", "engine": engine_name, "multiAz": False}
    multi = {**single, "multiAz": True}
    single_cost, _ = _compute(single, seed_catalog, backup_gb=50)
    multi_cost, engine = _compute(multi, seed_catalog, backup_gb=50)
    backup = 50 * 0.095
    assert single_cost == pytest.approx(730 * hourly + 20 * 0.115 + backup)
    assert multi_cost == pytest.approx(730 * hourly * 2 + 20 * 0.23 + backup)
    assert engine.unpriced_metrics == []


def test_multi_az_maps_the_rows_with_a_factor_of_two():
    metrics = RDSInstance().catalog_metrics_for(
        {"instanceClass": "db.m5.large", "engine": "postgres", "multiAz": True})
    assert metrics["instanceHours"] == {"RDS-Instance-Hour-postgres-db.m5.large": 2}
    assert metrics["storageGb"] == {"RDS-Storage-gp3": 2}
    assert metrics["backupStorageGb"] == "RDS-Backup-Storage"


def test_a_cloudformation_string_multi_az_counts():
    metrics = RDSInstance().catalog_metrics_for({"multiAz": "true"})
    assert metrics["instanceHours"] == {"RDS-Instance-Hour-db.t3.micro": 2}


def test_no_seed_multiplier_row_is_left_unused():
    """The handler states the factor, so no catalog row stands for it."""
    import json
    from infra_cost_model.pricing.cache import SEED_PRICES_PATH
    rows = json.loads(SEED_PRICES_PATH.read_text())
    assert not [r for r in rows if r["usage_metric"] == "RDS-Multi-AZ-Multiplier"]


@pytest.mark.parametrize("engine_name", ["oracle-ee", "sqlserver-ex", "aurora-mysql",
                                         "aurora-postgresql", "db2-se"])
def test_another_engine_is_reported_unpriced_not_billed_at_the_mysql_rate(
        seed_catalog, engine_name):
    config = {"instanceClass": "db.m5.large", "engine": engine_name}
    row = RDSInstance().catalog_metrics_for(config)["instanceHours"]
    assert engine_name in row
    assert not seed_catalog.query("aws", "AmazonRDS", "us-east-1", row)
    from infra_cost_model.pricing.sources import infracost as ic
    assert ic.descriptor_for(row) is None
    cost, engine = _compute(config, seed_catalog, storage_gb=0)
    assert cost == 0
    assert [u.node for u in engine.unpriced_metrics] == [ADDRESS]


def test_extraction_warns_about_an_engine_no_row_prices():
    resource = {"address": "aws_db_instance.legacy", "type": "aws_db_instance",
                "values": {"engine": "oracle-ee", "instance_class": "db.m5.large",
                           "region": "us-east-1"}}
    with pytest.warns(UserWarning, match="oracle-ee"):
        RDSInstance.extract_tf(resource)


@pytest.mark.parametrize("engine_name", [None, "mysql", "mariadb", "postgres"])
def test_extraction_is_silent_for_a_priced_engine(engine_name):
    import warnings
    resource = {"address": "aws_db_instance.db", "type": "aws_db_instance",
                "values": {"engine": engine_name, "instance_class": "db.t3.micro"}}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        RDSInstance.extract_tf(resource)


def test_cdk_and_pulumi_warn_too():
    with pytest.warns(UserWarning, match="sqlserver-se"):
        RDSInstance.extract_cdk({"LogicalId": "Db", "Properties": {
            "Engine": "sqlserver-se", "DBInstanceClass": "db.m5.large"}})
    with pytest.warns(UserWarning, match="oracle-se2"):
        RDSInstance.extract_pulumi({"id": "aws:rds:Instance:db", "inputs": {
            "engine": "oracle-se2", "instanceClass": "db.m5.large"}})
