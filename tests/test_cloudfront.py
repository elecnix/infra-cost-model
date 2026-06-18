"""Tests for Amazon CloudFront Distribution resource model (Issue #17)."""
import pytest
from infra_cost_model.resources.cloudfront import CloudFrontDistribution, _cloudfront_cost
from infra_cost_model.pricing.catalog import PricingCatalog

class TestCloudFrontAddressParsing:
    def test_from_address_terraform(self):
        r = CloudFrontDistribution.from_address("aws_cloudfront_distribution.cdn")
        assert r is not None and r.node_type == "routing"
    def test_from_address_pulumi(self):
        r = CloudFrontDistribution.from_address("aws.cloudfront.Distribution:my-cdn")
        assert r is not None and r.node_type == "routing"
    def test_from_address_cdk(self):
        r = CloudFrontDistribution.from_address("MyStack/MyCDN/CloudFront::Distribution")
        assert r is not None and r.node_type == "routing"
    def test_from_address_unrelated(self):
        assert CloudFrontDistribution.from_address("aws_s3_bucket.static") is None

class TestCloudFrontExtraction:
    def test_extract_tf(self):
        resource = {"address": "aws_cloudfront_distribution.cdn", "type": "aws_cloudfront_distribution", "values": {"aliases": ["cdn.example.com"], "price_class": "PriceClass_All", "enabled": True, "is_ipv6_enabled": True, "http_version": "http2", "origin": [{"origin_id": "S3-static", "domain_name": "static-bucket.s3.amazonaws.com", "origin_protocol_policy": "https-only"}, {"origin_id": "API", "domain_name": "api.example.com", "origin_protocol_policy": "https-only"}]}}
        result = CloudFrontDistribution.extract_tf(resource)
        assert result.node_type == "routing" and result.provider == "aws" and result.service == "AmazonCloudFront"
        assert result.region == "global" and result.config["priceClass"] == "PriceClass_All"
        assert len(result.config["origins"]) == 2
        assert result.config["origins"][0]["id"] == "S3-static"
    def test_extract_pulumi(self):
        resource = {"id": "aws.cloudfront.Distribution:cdn", "type": "aws.cloudfront.Distribution", "inputs": {"aliases": ["cdn.example.com"], "priceClass": "PriceClass_200", "enabled": True, "origins": [{"originId": "S3-origin", "domainName": "my-bucket.s3.amazonaws.com", "originProtocolPolicy": "https-only"}]}}
        result = CloudFrontDistribution.extract_pulumi(resource)
        assert result.config["priceClass"] == "PriceClass_200" and len(result.config["origins"]) == 1
    def test_extract_cdk(self):
        resource = {"Type": "AWS::CloudFront::Distribution", "LogicalId": "MyCDN", "Properties": {"DistributionConfig": {"Aliases": ["cdn.example.com"], "PriceClass": "PriceClass_100", "Enabled": True, "IPV6Enabled": True, "HttpVersion": "http2", "Origins": [{"Id": "S3Origin", "DomainName": "bucket.s3.amazonaws.com", "OriginProtocolPolicy": "https-only"}]}}}
        result = CloudFrontDistribution.extract_cdk(resource)
        assert result.config["priceClass"] == "PriceClass_100" and len(result.config["origins"]) == 1

class TestCloudFrontPricing:
    def setup_method(self): self.catalog = PricingCatalog()
    def test_http_request_pricing(self):
        cost = _cloudfront_cost(requests=1_000_000, https_ratio=0.0, catalog=self.catalog)
        assert cost == pytest.approx(0.75, rel=0.01)
    def test_https_request_pricing(self):
        cost = _cloudfront_cost(requests=1_000_000, https_ratio=1.0, catalog=self.catalog)
        assert cost == pytest.approx(1.00, rel=0.01)
    def test_https_more_expensive_than_http(self):
        http = _cloudfront_cost(requests=1_000_000, https_ratio=0.0, catalog=self.catalog)
        https = _cloudfront_cost(requests=1_000_000, https_ratio=1.0, catalog=self.catalog)
        assert https > http
    def test_mixed_http_https(self):
        cost = _cloudfront_cost(requests=1_000_000, https_ratio=0.8, catalog=self.catalog)
        assert cost == pytest.approx(0.95, rel=0.01)
    def test_data_transfer_first_tier(self):
        cost = _cloudfront_cost(data_out_gb=5000, catalog=self.catalog)
        assert cost == pytest.approx(425.00, rel=0.01)
    def test_data_transfer_crossing_tiers(self):
        cost = _cloudfront_cost(data_out_gb=15000, catalog=self.catalog)
        expected = 10240 * 0.085 + 4760 * 0.080
        assert cost == pytest.approx(expected, rel=0.01)
    def test_origin_requests_s3(self):
        cost = _cloudfront_cost(origin_requests=500_000, origin_is_s3=True, catalog=self.catalog)
        assert cost == pytest.approx(0.375, rel=0.01)
    def test_origin_requests_custom(self):
        cost = _cloudfront_cost(origin_requests=500_000, origin_is_s3=False, catalog=self.catalog)
        assert cost == pytest.approx(0.60, rel=0.01)
    def test_origin_custom_more_expensive_than_s3(self):
        s3_cost = _cloudfront_cost(origin_requests=1_000_000, origin_is_s3=True, catalog=self.catalog)
        custom_cost = _cloudfront_cost(origin_requests=1_000_000, origin_is_s3=False, catalog=self.catalog)
        assert custom_cost > s3_cost
    def test_combined_all_dimensions(self):
        cost = _cloudfront_cost(requests=10_000_000, https_ratio=0.5, data_out_gb=500, origin_requests=1_000_000, origin_is_s3=True, catalog=self.catalog)
        expected = 3.75 + 5.00 + 42.50 + 0.75
        assert cost == pytest.approx(expected, rel=0.01)
    def test_zero_usage(self):
        assert _cloudfront_cost(catalog=self.catalog) == 0.0

class TestCloudFrontRoutingNode:
    def test_is_routing_node(self):
        assert CloudFrontDistribution.from_address("aws_cloudfront_distribution.cdn").node_type == "routing"
    def test_valid_metrics(self):
        d = CloudFrontDistribution()
        assert all(m in d.valid_metrics for m in ["requests", "dataOutGb", "originRequests"])

class TestCloudFrontRegistryIntegration:
    def test_in_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        assert ResourceRegistry.from_address("aws_cloudfront_distribution.cdn") == CloudFrontDistribution
    def test_extract_via_registry(self):
        from infra_cost_model.resources.registry import ResourceRegistry
        resource = {"address": "aws_cloudfront_distribution.cdn", "type": "aws_cloudfront_distribution", "values": {"aliases": ["cdn.example.com"], "price_class": "PriceClass_All", "origin": [{"origin_id": "S3", "domain_name": "bucket.s3.amazonaws.com"}]}}
        result = ResourceRegistry.extract("aws_cloudfront_distribution.cdn", resource, "terraform")
        assert result is not None and result["provider"] == "aws" and result["service"] == "AmazonCloudFront" and result["nodeType"] == "routing"
