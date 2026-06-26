"""Tests for Amazon ECS Fargate Service resource model (Issue #182)."""
import pytest
from infra_cost_model.resources.ecs import ECSFargateService, _ecs_fargate_cost
from infra_cost_model.pricing.catalog import PricingCatalog


class TestECSAddressParsing:
    def test_from_address_terraform_service(self):
        r = ECSFargateService.from_address("aws_ecs_service.main")
        assert r is not None and r.node_type == "compute"

    def test_from_address_terraform_task_definition(self):
        r = ECSFargateService.from_address("aws_ecs_task_definition.app")
        assert r is not None and r.node_type == "compute"

    def test_from_address_pulumi(self):
        r = ECSFargateService.from_address("aws.ecs.Service:app-service")
        assert r is not None and r.node_type == "compute"

    def test_from_address_cdk(self):
        r = ECSFargateService.from_address("AppStack/Service/ECS::Service")
        assert r is not None and r.node_type == "compute"

    def test_from_address_unrelated(self):
        assert ECSFargateService.from_address("aws_lambda_function.handler") is None


class TestECSExtraction:
    def test_extract_tf(self):
        resource = {
            "address": "aws_ecs_service.api",
            "type": "aws_ecs_service",
            "values": {
                "desired_count": 2,
                "launch_type": "FARGATE",
                "cpu": "512",
                "memory": "1024",
                "region": "us-east-1",
                "runtime_platform": {
                    "cpu_architecture": "X86_64",
                },
                "ephemeral_storage": {
                    "size_in_gib": 21,
                },
            },
        }
        result = ECSFargateService.extract_tf(resource)
        assert result.node_type == "compute" and result.provider == "aws" and result.service == "AmazonECS"
        assert result.config["desiredCount"] == 2
        assert result.config["launchType"] == "FARGATE"
        assert result.config["cpu"] == "512"
        assert result.config["memory"] == "1024"
        assert result.config["cpuArchitecture"] == "X86_64"
        assert result.config["ephemeralStorageGb"] == 21

    def test_extract_tf_defaults(self):
        resource = {
            "address": "aws_ecs_service.minimal",
            "type": "aws_ecs_service",
            "values": {},
        }
        result = ECSFargateService.extract_tf(resource)
        assert result.config["desiredCount"] == 1
        assert result.config["cpu"] == "256"
        assert result.config["memory"] == "512"
        assert result.config["cpuArchitecture"] == "X86_64"
        assert result.config["ephemeralStorageGb"] == 20

    def test_extract_tf_arm(self):
        resource = {
            "address": "aws_ecs_service.arm",
            "type": "aws_ecs_service",
            "values": {
                "runtime_platform": {
                    "cpu_architecture": "ARM64",
                },
            },
        }
        result = ECSFargateService.extract_tf(resource)
        assert result.config["cpuArchitecture"] == "ARM64"

    def test_extract_pulumi(self):
        resource = {
            "id": "aws.ecs.Service:web-svc",
            "type": "aws.ecs.Service",
            "inputs": {
                "desiredCount": 3,
                "launchType": "FARGATE",
                "cpu": "1024",
                "memory": "2048",
                "runtimePlatform": {
                    "cpuArchitecture": "ARM64",
                },
            },
        }
        result = ECSFargateService.extract_pulumi(resource)
        assert result.provider == "aws" and result.config["desiredCount"] == 3
        assert result.config["cpu"] == "1024" and result.config["memory"] == "2048"
        assert result.config["cpuArchitecture"] == "ARM64"

    def test_extract_cdk(self):
        resource = {
            "Type": "AWS::ECS::Service",
            "LogicalId": "ApiService",
            "Properties": {
                "DesiredCount": 1,
                "LaunchType": "FARGATE",
                "Cpu": "256",
                "Memory": "512",
            },
        }
        result = ECSFargateService.extract_cdk(resource)
        assert result.config["desiredCount"] == 1
        assert result.config["cpu"] == "256" and result.config["memory"] == "512"


class TestECSPricing:
    def setup_method(self):
        self.catalog = PricingCatalog()

    def test_vcpu_cost_x86_one_task(self):
        # 1 task * 730 hours * 0.25 vCPU = 182.5 vCPU-hours
        # 182.5 * $0.04048 = $7.3876
        cost = _ecs_fargate_cost(
            task_count=1, hours=730, cpu="256", memory="0",
            ephemeral_storage_gb=0, catalog=self.catalog, region="us-east-1",
        )
        assert cost == pytest.approx(7.3876, rel=0.01)

    def test_memory_cost_x86_one_task(self):
        # 1 task * 730 hours * 0.5 GB = 365 GB-hours
        # 365 * $0.004445 = $1.6224
        cost = _ecs_fargate_cost(
            task_count=1, hours=730, cpu="0", memory="512",
            ephemeral_storage_gb=0, catalog=self.catalog, region="us-east-1",
        )
        assert cost == pytest.approx(1.6224, rel=0.01)

    def test_combined_vcpu_and_memory(self):
        # vCPU: 1 * 730 * 0.5 = 365 vCPU-hours * 0.04048 = $14.7752
        # mem:  1 * 730 * 1.0 = 730 GB-hours * 0.004445 = $3.24485
        # total = $18.02
        cost = _ecs_fargate_cost(
            task_count=1, hours=730, cpu="512", memory="1024",
            ephemeral_storage_gb=0, catalog=self.catalog, region="us-east-1",
        )
        assert cost == pytest.approx(18.02, rel=0.01)

    def test_arm_is_cheaper_than_x86(self):
        # ARM vCPU-hour is $0.03238 vs $0.04048 for X86 (~20% cheaper)
        # Same GB-hour comparison
        cost_arm = _ecs_fargate_cost(
            task_count=1, hours=730, cpu="1024", memory="2048",
            cpu_architecture="ARM64", ephemeral_storage_gb=0,
            catalog=self.catalog, region="us-east-1",
        )
        cost_x86 = _ecs_fargate_cost(
            task_count=1, hours=730, cpu="1024", memory="2048",
            cpu_architecture="X86_64", ephemeral_storage_gb=0,
            catalog=self.catalog, region="us-east-1",
        )
        assert cost_x86 > cost_arm
        assert cost_arm == pytest.approx(cost_x86 * 0.8, rel=0.02)

    def test_ephemeral_storage_beyond_free_tier(self):
        # 2 tasks * 25 GB each = 50 GB total, free tier = 40 GB, excess = 10 GB
        # 10 GB * $0.081 = $0.81
        cost = _ecs_fargate_cost(
            task_count=2, hours=730, cpu="0", memory="0",
            ephemeral_storage_gb=25, catalog=self.catalog, region="us-east-1",
        )
        assert cost == pytest.approx(0.81, rel=0.01)

    def test_ephemeral_storage_within_free_tier(self):
        # 1 task * 20 GB = within free tier, $0
        cost = _ecs_fargate_cost(
            task_count=1, hours=730, cpu="0", memory="0",
            ephemeral_storage_gb=20, catalog=self.catalog, region="us-east-1",
        )
        assert cost == 0.0

    def test_multiple_tasks(self):
        # 3 tasks * 730h * 0.25 vCPU = 547.5 vCPU-hours * 0.04048 = $22.1628
        # 3 tasks * 730h * 0.5 GB = 1095 GB-hours * 0.004445 = $4.8673
        # total = $27.03
        cost = _ecs_fargate_cost(
            task_count=3, hours=730, cpu="256", memory="512",
            ephemeral_storage_gb=0, catalog=self.catalog, region="us-east-1",
        )
        assert cost == pytest.approx(27.03, rel=0.01)

    def test_zero_usage(self):
        assert _ecs_fargate_cost(
            task_count=0, hours=730, cpu="0", memory="0",
            ephemeral_storage_gb=0, catalog=self.catalog, region="us-east-1",
        ) == 0.0


class TestECSNodeType:
    def test_ecs_is_compute_node(self):
        result = ECSFargateService.from_address("aws_ecs_service.test")
        assert result is not None and result.node_type == "compute"
        from infra_cost_model.resources.registry import is_leaf_node
        assert is_leaf_node("compute") is False and is_leaf_node(result.node_type) is False

    def test_ecs_valid_metrics(self):
        i = ECSFargateService()
        assert all(m in i.valid_metrics for m in ["vCpuHours", "gbHours", "ephemeralStorageGb"])


class TestECSRegistryIntegration:
    def test_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_ecs_service.main") == ECSFargateService

    def test_task_definition_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_ecs_task_definition.app") == ECSFargateService

    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {
            "address": "aws_ecs_service.main",
            "type": "aws_ecs_service",
            "values": {
                "desired_count": 1,
                "launch_type": "FARGATE",
                "cpu": "256",
                "memory": "512",
                "region": "us-east-1",
            },
        }
        result = ResourceRegistry.extract("aws_ecs_service.main", resource, "terraform")
        assert result is not None
        assert result["provider"] == "aws"
        assert result["service"] == "AmazonECS"
        assert result["nodeType"] == "compute"
