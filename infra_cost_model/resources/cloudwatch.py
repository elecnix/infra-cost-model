"""Amazon CloudWatch Logs resource model.

CloudWatch Logs is a storage (leaf) node with two cost dimensions:
- Log ingestion: $0.50 per GB ingested (the dominant term; scales with request volume)
- Log storage: $0.03 per GB-month (retained volume, driven by retention_in_days)

AWS gives the account 5 GB of ingestion and 5 GB-month of storage free each
month, once across all regions (#342). The seed rows state both allowances.
"""

from typing import Optional
from .types import StorageResource, ResourceExtract


class CloudWatchLogGroup(StorageResource):
    """Amazon CloudWatch Log Group - storage node (leaf, no outgoing edges)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["ingestedGb", "storedGb"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"ingestedGb": "CloudWatch-Log-Ingestion",
                "storedGb": "CloudWatch-Log-Storage"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudWatchLogGroup"]:
        if (resource_address.startswith("aws_cloudwatch_log_group.") or
                resource_address.startswith("aws.cloudwatch.LogGroup:") or
                resource_address.startswith("aws:cloudwatch:LogGroup:") or
                "Logs::LogGroup:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=values.get("region"),
            config={
                "name": values.get("name"),
                "retentionInDays": values.get("retention_in_days", 0),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "retentionInDays": inputs.get("retentionInDays", 0),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=None,
            config={
                "name": properties.get("LogGroupName"),
                "retentionInDays": properties.get("RetentionInDays", 0),
            },
        )


class CloudWatchMetricAlarm(StorageResource):
    """Amazon CloudWatch Metric Alarm - storage node (leaf, no outgoing edges).

    Models the recurring CloudWatch metrics/alarms cost dimensions:
    - Custom metrics: $0.30 per metric-month for the first 10,000, then less
    - Alarms: $0.10 per standard-resolution alarm metric-month
    - GetMetricData: $0.00001 per metric requested, with no free tier (#341)

    The account gets 10 metrics and 10 alarm metrics free each month, once
    across all regions (#342).
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["alarmsCount", "customMetricsCount", "getMetricDataRequests"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        return {"alarmsCount": "CloudWatch-Alarm-Month",
                "customMetricsCount": "CloudWatch-Metric-Month",
                "getMetricDataRequests": "CloudWatch-GetMetricData"}

    @classmethod
    def from_address(cls, resource_address: str) -> Optional["CloudWatchMetricAlarm"]:
        if (resource_address.startswith("aws_cloudwatch_metric_alarm.") or
                resource_address.startswith("aws.cloudwatch.MetricAlarm:") or
                resource_address.startswith("aws:cloudwatch:MetricAlarm:") or
                "CloudWatch::Alarm:" in resource_address):
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        values = resource.get("values", {})
        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=values.get("region"),
            config={
                "name": values.get("alarm_name"),
                "namespace": values.get("namespace"),
                "metricName": values.get("metric_name"),
                "comparisonOperator": values.get("comparison_operator"),
                "period": values.get("period", 0),
            },
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        inputs = resource.get("inputs", {})
        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=inputs.get("region"),
            config={
                "name": inputs.get("name"),
                "namespace": inputs.get("namespace"),
                "metricName": inputs.get("metricName"),
                "comparisonOperator": inputs.get("comparisonOperator"),
                "period": inputs.get("period", 0),
            },
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        properties = resource.get("Properties", {})
        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage", provider="aws", service="AmazonCloudWatch",
            region=None,
            config={
                "name": properties.get("AlarmName"),
                "namespace": properties.get("Namespace"),
                "metricName": properties.get("MetricName"),
                "comparisonOperator": properties.get("ComparisonOperator"),
                "period": properties.get("Period", 0),
            },
        )
