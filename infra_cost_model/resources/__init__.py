"""Resource types package.

Public API surface is limited to resource classes and computation utilities.
Each handler declares the catalog row that prices each of its logical usage
metrics, and the engine resolves those rows through the DAG, so usage is
derived from the graph rather than specified as free variables (DP#1).

Multi-cloud support (DP#6): AWS, GCP, and Azure resource handlers are
registered through the ResourceRegistry with provider-based dispatch.
"""

from .types import ResourceType, ComputeResource, StorageResource, RoutingResource, ExternalResource
from .lambda_func import LambdaFunction, calculate_gb_seconds
from .external import ExternalNode, ExternalServiceRegistry
from .apigw import APIGatewayHTTP
from .dynamodb import DynamoDBTable
from .bedrock import BedrockModel
from .s3 import S3Bucket
from .sqs import SQSQueue
from .sns import SNSTopic
from .eventbridge import EventBridgeRule
from .cloudfront import CloudFrontDistribution
from .networking import NATGateway, VpcEndpoint, ElasticIP
from .rds import RDSInstance
from .cloudwatch import CloudWatchLogGroup, CloudWatchMetricAlarm
from .ecs import ECSFargateService
from .alb import ApplicationLoadBalancer
from .gcp import CloudFunction, CloudStorage, CloudRun, Firestore
from .azure import AzureFunction, CosmosDB, APIManagement, AzureOpenAI, AzureBlobStorage
from .misc_services import SecretsManagerSecret, ECRRepository, Route53Zone
from .kms import KMSKey
from .data_transfer import DataTransferNode

__all__ = [
    "ResourceType",
    "ComputeResource",
    "StorageResource",
    "RoutingResource",
    "ExternalResource",
    "LambdaFunction",
    "calculate_gb_seconds",
    "ExternalNode",
    "ExternalServiceRegistry",
    "APIGatewayHTTP",
    "DynamoDBTable",
    "BedrockModel",
    "S3Bucket",
    "SQSQueue",
    "SNSTopic",
    "EventBridgeRule",
    "CloudFrontDistribution",
    "NATGateway",
    "VpcEndpoint",
    "ElasticIP",
    "RDSInstance",
    "CloudWatchLogGroup",
    "CloudWatchMetricAlarm",
    "ECSFargateService",
    "ApplicationLoadBalancer",
    "CloudFunction",
    "CloudStorage",
    "CloudRun",
    "Firestore",
    "AzureFunction",
    "CosmosDB",
    "APIManagement",
    "AzureOpenAI",
    "AzureBlobStorage",
    "SecretsManagerSecret",
    "ECRRepository",
    "Route53Zone",
    "KMSKey",
    "DataTransferNode",
]
