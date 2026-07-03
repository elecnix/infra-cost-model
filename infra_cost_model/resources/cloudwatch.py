"""Amazon CloudWatch Logs resource model.

CloudWatch Logs is a storage (leaf) node with two cost dimensions:
- Log ingestion: $0.50 per GB ingested (the dominant term; scales with request volume)
- Log storage: $0.03 per GB-month (retained volume, driven by retention_in_days)

Actual AWS pricing includes a 5 GB per-account storage free tier. We model
conservatively without the free tier by default.
"""

from typing import Optional
from infra_cost_model.pricing.catalog import PricingCatalog
from .types import StorageResource, ResourceExtract


class CloudWatchLogGroup(StorageResource):
    """Amazon CloudWatch Log Group - storage node (leaf, no outgoing edges)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["ingestedGb", "storedGb"]

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
    - Custom metrics: $0.30 per metric-month (standard resolution)
    - Alarms: $0.10 per standard-resolution alarm-month
    - GetMetricData: $0.00001 per metric requested (after a 1M free tier)
    """

    @property
    def valid_metrics(self) -> list[str]:
        return ["alarmsCount", "customMetricsCount", "getMetricDataRequests"]

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


def _cloudwatch_log_cost(ingested_gb=0.0, stored_gb=0.0, *,
                         catalog=None, provider: str = "aws",
                         region: str = "us-east-1") -> float:
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if ingested_gb > 0:
        r = catalog.query(provider, "AmazonCloudWatch", region,
                          "CloudWatch-Log-Ingestion", ingested_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if stored_gb > 0:
        r = catalog.query(provider, "AmazonCloudWatch", region,
                          "CloudWatch-Log-Storage", stored_gb)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total


def _cloudwatch_metric_cost(custom_metrics_count=0, alarms_count=0,
                            get_metric_data_requests=0, *,
                            catalog=None, provider: str = "aws",
                            region: str = "us-east-1") -> float:
    """Monthly cost for CloudWatch custom metrics, alarms, and GetMetricData.

    - custom_metrics_count: number of custom metrics ($0.30 per metric-month)
    - alarms_count: number of standard-resolution alarms ($0.10 per alarm-month)
    - get_metric_data_requests: metrics requested via GetMetricData
      ($0.00001 each, after a 1,000,000 free tier)
    """
    if catalog is None:
        catalog = PricingCatalog()
    total = 0.0
    if custom_metrics_count > 0:
        r = catalog.query(provider, "AmazonCloudWatch", region,
                          "CloudWatch-Metric-Month", custom_metrics_count)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if alarms_count > 0:
        r = catalog.query(provider, "AmazonCloudWatch", region,
                          "CloudWatch-Alarm-Month", alarms_count)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    if get_metric_data_requests > 0:
        r = catalog.query(provider, "AmazonCloudWatch", region,
                          "CloudWatch-GetMetricData", get_metric_data_requests)
        if r and hasattr(r, "total_cost"):
            total += r.total_cost
    return total
