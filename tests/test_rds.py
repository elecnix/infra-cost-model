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
                 instance_class=None, multi_az=False):
            config = {"instanceClass": instance_class} if instance_class else None
            total = resource_cost("aws_db_instance.db", "AmazonRDS", "us-east-1",
                                  catalog=self.catalog, config=config,
                                  instanceHours=instance_hours,
                                  storageGb=storage_gb,
                                  backupStorageGb=backup_storage_gb)
            if multi_az and instance_hours:
                # A standby doubles the instance charge. The handler declares no
                # logical metric for the multiplier, so read the row directly.
                multiplier = self.catalog.query(
                    "aws", "AmazonRDS", "us-east-1", "RDS-Multi-AZ-Multiplier"
                ).price_usd
                total += (total - resource_cost(
                    "aws_db_instance.db", "AmazonRDS", "us-east-1",
                    catalog=self.catalog, config=config,
                    storageGb=storage_gb, backupStorageGb=backup_storage_gb,
                )) * (multiplier - 1)
            return total

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

    def test_multi_az_doubles_instance_cost(self):
        single = self.cost(instance_hours=730, instance_class="db.t3.micro", storage_gb=20)
        multi = self.cost(instance_hours=730, instance_class="db.t3.micro",
                          storage_gb=20, multi_az=True)
        expected = 12.41 * 2 + 2.30
        assert multi == pytest.approx(expected, rel=0.01) and multi > single

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
