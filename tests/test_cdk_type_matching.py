"""CDK CloudFormation-type matching precision tests.

`extract_resources_from_cdk` builds synthetic addresses as
``f"{resource_type}:{logical_id}"`` (e.g. ``AWS::S3::Bucket:MyBucket``), so a
handler's CFN-type check must require the type to be immediately followed by
``:``. A loose ``"S3::Bucket" in addr`` substring test would otherwise
mis-match sibling resources like ``AWS::S3::BucketPolicy`` and count a
non-priced policy/attachment resource as its priced parent.

Each case below pairs a real address (must match) with a sibling type that
shares the parent's prefix (must NOT match).
"""
import pytest

from infra_cost_model.resources.s3 import S3Bucket
from infra_cost_model.resources.sns import SNSTopic
from infra_cost_model.resources.sqs import SQSQueue
from infra_cost_model.resources.apigw import APIGatewayHTTP
from infra_cost_model.resources.lambda_func import LambdaFunction
from infra_cost_model.resources.dynamodb import DynamoDBTable
from infra_cost_model.resources.bedrock import BedrockModel
from infra_cost_model.resources.misc_services import SecretsManagerSecret
from infra_cost_model.resources.networking import NATGateway, VpcEndpoint, ElasticIP
from infra_cost_model.resources.cloudwatch import CloudWatchLogGroup, CloudWatchMetricAlarm
from infra_cost_model.resources.kms import KMSKey


# (handler, matching CDK address, colliding sibling address that must be excluded)
CASES = [
    (S3Bucket, "AWS::S3::Bucket:MyBucket", "AWS::S3::BucketPolicy:MyBucketPolicy"),
    (SNSTopic, "AWS::SNS::Topic:MyTopic", "AWS::SNS::TopicPolicy:MyTopicPolicy"),
    (SQSQueue, "AWS::SQS::Queue:MyQueue", "AWS::SQS::QueuePolicy:MyQueuePolicy"),
    (APIGatewayHTTP, "AWS::ApiGatewayV2::Api:MyApi", "AWS::ApiGatewayV2::ApiMapping:MyMapping"),
    (LambdaFunction, "AWS::Lambda::Function:MyFn", "AWS::Lambda::FunctionUrl:MyUrl"),
    (DynamoDBTable, "AWS::DynamoDB::Table:MyTable", "AWS::DynamoDB::GlobalTable:MyGlobal"),
    (BedrockModel, "AWS::Bedrock::Model:MyModel",
     "AWS::Bedrock::ModelInvocationLoggingConfiguration:MyLogging"),
    (SecretsManagerSecret, "AWS::SecretsManager::Secret:MySecret",
     "AWS::SecretsManager::SecretTargetAttachment:MyAttach"),
    (VpcEndpoint, "AWS::EC2::VPCEndpoint:MyVpce", "AWS::EC2::VPCEndpointService:MySvc"),
    (ElasticIP, "AWS::EC2::EIP:MyEip", "AWS::EC2::EIPAssociation:MyAssoc"),
    (CloudWatchMetricAlarm, "AWS::CloudWatch::Alarm:MyAlarm",
     "AWS::CloudWatch::CompositeAlarm:MyComposite"),
    (KMSKey, "AWS::KMS::Key:MyKey", "AWS::KMS::ReplicaKey:MyReplica"),
]


@pytest.mark.parametrize("handler,match_addr,collision_addr", CASES,
                         ids=[c[0].__name__ for c in CASES])
def test_cdk_type_matches_parent_not_sibling(handler, match_addr, collision_addr):
    assert handler.from_address(match_addr) is not None, (
        f"{handler.__name__} should match its own CDK address {match_addr!r}")
    assert handler.from_address(collision_addr) is None, (
        f"{handler.__name__} must NOT match sibling type {collision_addr!r}")


def test_registry_dispatches_bucket_not_bucket_policy():
    """End-to-end: the registry routes the parent type but not the sibling."""
    from infra_cost_model.resources.registry import ResourceRegistry
    assert ResourceRegistry.from_address("AWS::S3::Bucket:MyBucket") is S3Bucket
    # A BucketPolicy has no registered handler and must not resolve to S3Bucket.
    assert ResourceRegistry.from_address("AWS::S3::BucketPolicy:MyPolicy") is not S3Bucket
