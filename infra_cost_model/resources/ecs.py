"""Amazon ECS Fargate Service resource model.

ECS Fargate is always-on container compute. Pricing dimensions:
- vCPU-hours: task.cpu / 1024 vCPUs * running hours
- GB-hours: task.memory / 1024 GB * running hours
- Ephemeral storage: per GB-month beyond 20 GB free tier per task
- ARM/Graviton architecture is ~20% cheaper than X86_64
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import ComputeResource, ResourceExtract


class ECSFargateService(ComputeResource):
    """Amazon ECS Fargate Service - always-on compute node."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["vCpuHours", "gbHours", "ephemeralStorageGb"]

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["ECSFargateService"]:
        if (resource_address.startswith("aws_ecs_service.") or
                resource_address.startswith("aws_ecs_task_definition.") or
                resource_address.startswith("aws.ecs.Service:") or
                "ECS::Service" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        runtime_platform = values.get("runtime_platform", {})
        ephemeral_storage = values.get("ephemeral_storage", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="compute", provider="aws", service="AmazonECS",
            region=values.get("region"),
            config={
                "desiredCount": values.get("desired_count", 1),
                "launchType": values.get("launch_type", "FARGATE"),
                "cpu": values.get("cpu", "256"),
                "memory": values.get("memory", "512"),
                "cpuArchitecture": runtime_platform.get("cpu_architecture", "X86_64")
                if isinstance(runtime_platform, dict) else "X86_64",
                "ephemeralStorageGb": ephemeral_storage.get("size_in_gib", 20)
                if isinstance(ephemeral_storage, dict) else 20,
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        runtime_platform = inputs.get("runtimePlatform", {})
        ephemeral_storage = inputs.get("ephemeralStorage", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="compute", provider="aws", service="AmazonECS",
            region=None,
            config={
                "desiredCount": inputs.get("desiredCount", 1),
                "launchType": inputs.get("launchType", "FARGATE"),
                "cpu": inputs.get("cpu", "256"),
                "memory": inputs.get("memory", "512"),
                "cpuArchitecture": runtime_platform.get("cpuArchitecture", "X86_64")
                if isinstance(runtime_platform, dict) else "X86_64",
                "ephemeralStorageGb": ephemeral_storage.get("sizeInGib", 20)
                if isinstance(ephemeral_storage, dict) else 20,
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        runtime_platform = properties.get("RuntimePlatform", {})
        ephemeral_storage = properties.get("EphemeralStorage", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="compute", provider="aws", service="AmazonECS",
            region=None,
            config={
                "desiredCount": properties.get("DesiredCount", 1),
                "launchType": properties.get("LaunchType", "FARGATE"),
                "cpu": properties.get("Cpu", "256"),
                "memory": properties.get("Memory", "512"),
                "cpuArchitecture": runtime_platform.get("CpuArchitecture", "X86_64")
                if isinstance(runtime_platform, dict) else "X86_64",
                "ephemeralStorageGb": ephemeral_storage.get("SizeInGiB", 20)
                if isinstance(ephemeral_storage, dict) else 20,
            },
        )


def _ecs_fargate_cost(task_count=1, hours=730, cpu="256", memory="512",
                       cpu_architecture="X86_64", ephemeral_storage_gb=20,
                       *, catalog=None, provider: str = "aws", region: str) -> float:
    """Calculate ECS Fargate monthly cost.

    Args:
        task_count: Number of running tasks (from desired_count)
        hours: Hours in billing period (default 730 for a month)
        cpu: Task CPU units (1024 = 1 vCPU)
        memory: Task memory in MB (1024 = 1 GB)
        cpu_architecture: "X86_64" or "ARM64" (ARM ~20% cheaper)
        ephemeral_storage_gb: Ephemeral storage per task, 20 GB free tier
        catalog: PricingCatalog instance
        provider: Cloud provider (default "aws")
        region: AWS region

    Returns:
        Total monthly cost in USD
    """
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0

    is_arm = cpu_architecture == "ARM64"
    vcpu_metric = "ECS-Fargate-vCPU-Hour-ARM" if is_arm else "ECS-Fargate-vCPU-Hour"
    gb_metric = "ECS-Fargate-GB-Hour-ARM" if is_arm else "ECS-Fargate-GB-Hour"

    # vCPU cost: task_count * hours * vCPU count
    vcpu = float(cpu) / 1024.0
    vcpu_hours = task_count * hours * vcpu
    r = catalog.query(provider, "AmazonECS", region, vcpu_metric, vcpu_hours)
    if r and hasattr(r, "total_cost"):
        total += r.total_cost

    # GB cost: task_count * hours * GB count
    mem_gb = float(memory) / 1024.0
    gb_hours = task_count * hours * mem_gb
    r = catalog.query(provider, "AmazonECS", region, gb_metric, gb_hours)
    if r and hasattr(r, "total_cost"):
        total += r.total_cost

    # Ephemeral storage: 20 GB free tier per task
    free_tier_gb = 20 * task_count
    excess_gb = max(0, ephemeral_storage_gb * task_count - free_tier_gb)
    if excess_gb > 0:
        r = catalog.query(provider, "AmazonECS", region, "ECS-Fargate-Ephemeral-Storage", excess_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost

    return total
