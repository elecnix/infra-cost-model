"""Amazon ECS Fargate Service resource model.

ECS Fargate is always-on container compute. Pricing dimensions:
- vCPU-hours: task.cpu / 1024 vCPUs * running hours
- GB-hours: task.memory / 1024 GB * running hours
- Ephemeral storage: per GB-month beyond 20 GB free tier per task
- ARM/Graviton architecture is ~20% cheaper than X86_64
"""

from typing import Optional
from .types import ComputeResource, ResourceExtract


class ECSFargateService(ComputeResource):
    """Amazon ECS Fargate Service - always-on compute node."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["vCpuHours", "gbHours", "ephemeralStorageGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"vCpuHours": "ECS-Fargate-vCPU-Hour",
                "gbHours": "ECS-Fargate-GB-Hour",
                "ephemeralStorageGb": "ECS-Fargate-Ephemeral-Storage"}

    def catalog_metrics_for(self, config: dict) -> dict[str, str]:
        """ARM/Graviton is a separate, cheaper row for both compute dimensions."""
        if (config or {}).get("cpuArchitecture") != "ARM64":
            return self.catalog_metrics
        return {"vCpuHours": "ECS-Fargate-vCPU-Hour-ARM",
                "gbHours": "ECS-Fargate-GB-Hour-ARM",
                "ephemeralStorageGb": "ECS-Fargate-Ephemeral-Storage"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["ECSFargateService"]:
        if (resource_address.startswith("aws_ecs_service.") or
                resource_address.startswith("aws_ecs_task_definition.") or
                resource_address.startswith("aws.ecs.Service:") or
                "ECS::Service:" in resource_address):
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
