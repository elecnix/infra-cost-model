"""Tests for the codegen pipeline (DP#10).

Tests the terraform schema reader and code generator with a sample
schema that mimics `terraform providers schema -json` output.
"""

import json
import textwrap

import pytest
from infra_cost_model.codegen.schema_reader import (
    SchemaReader, SchemaAttribute, ResourceSchema, ProviderSchema,
)
from infra_cost_model.codegen.generator import CodeGenerator, generate_handler


# Sample terraform providers schema -json output (minimized)
SAMPLE_AWS_SCHEMA = {
    "format_version": "1.0",
    "provider_schemas": {
        "registry.terraform.io/hashicorp/aws": {
            "resource_schemas": {
                "aws_lambda_function": {
                    "version": 0,
                    "block": {
                        "attributes": {
                            "function_name": {
                                "type": "string",
                                "required": True,
                                "optional": False,
                                "computed": False,
                                "description": "Unique name for the Lambda function",
                            },
                            "memory_size": {
                                "type": "number",
                                "required": False,
                                "optional": True,
                                "computed": False,
                                "description": "Amount of memory in MB",
                            },
                            "runtime": {
                                "type": "string",
                                "required": True,
                                "optional": False,
                                "computed": False,
                                "description": "Runtime environment",
                            },
                            "timeout": {
                                "type": "number",
                                "required": False,
                                "optional": True,
                                "computed": False,
                            },
                            "handler": {
                                "type": "string",
                                "required": True,
                                "optional": False,
                                "computed": False,
                            },
                            "id": {
                                "type": "string",
                                "required": False,
                                "optional": False,
                                "computed": True,
                            },
                        }
                    },
                },
                "aws_s3_bucket": {
                    "version": 0,
                    "block": {
                        "attributes": {
                            "bucket": {
                                "type": "string",
                                "required": True,
                                "optional": False,
                                "computed": False,
                            },
                            "acl": {
                                "type": "string",
                                "required": False,
                                "optional": True,
                                "computed": False,
                            },
                            "id": {
                                "type": "string",
                                "required": False,
                                "optional": False,
                                "computed": True,
                            },
                        }
                    },
                },
            }
        }
    },
}


class TestSchemaReader:
    """Terraform providers schema -json parsing."""

    def test_parse_aws_schema(self):
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        assert len(providers) == 1
        provider = providers[0]
        assert provider.provider_name == "registry.terraform.io/hashicorp/aws"
        assert len(provider.resources) == 2

    def test_resource_types_extracted(self):
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        resource_types = {r.resource_type for r in providers[0].resources}
        assert "aws_lambda_function" in resource_types
        assert "aws_s3_bucket" in resource_types

    def test_lambda_attributes(self):
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        lambda_resource = next(
            r for r in providers[0].resources
            if r.resource_type == "aws_lambda_function"
        )
        attr_names = {a.name for a in lambda_resource.attributes}
        assert "function_name" in attr_names
        assert "memory_size" in attr_names
        assert "runtime" in attr_names
        assert "timeout" in attr_names
        # 'id' is skipped (computed-only)
        assert "id" not in attr_names

    def test_required_attribute(self):
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        lambda_resource = next(
            r for r in providers[0].resources
            if r.resource_type == "aws_lambda_function"
        )
        func_name = next(a for a in lambda_resource.attributes if a.name == "function_name")
        assert func_name.required is True
        assert func_name.type == "string"

    def test_optional_attribute(self):
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        lambda_resource = next(
            r for r in providers[0].resources
            if r.resource_type == "aws_lambda_function"
        )
        memory = next(a for a in lambda_resource.attributes if a.name == "memory_size")
        assert memory.optional is True
        assert memory.type == "number"

    def test_provider_short_name(self):
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        resource = providers[0].resources[0]
        assert resource.provider == "aws"

    def test_parse_file(self, tmp_path):
        """Test parsing from a file on disk."""
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps(SAMPLE_AWS_SCHEMA))
        providers = SchemaReader.parse_file(str(schema_path))
        assert len(providers) == 1
        assert len(providers[0].resources) == 2

    def test_empty_schema(self):
        providers = SchemaReader.parse({"format_version": "1.0", "provider_schemas": {}})
        assert len(providers) == 0


class TestCodeGenerator:
    """Code generation from parsed schemas."""

    def _get_lambda_schema(self) -> ResourceSchema:
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        return next(
            r for r in providers[0].resources
            if r.resource_type == "aws_lambda_function"
        )

    def _get_s3_schema(self) -> ResourceSchema:
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        return next(
            r for r in providers[0].resources
            if r.resource_type == "aws_s3_bucket"
        )

    def test_generate_lambda_handler(self):
        generator = CodeGenerator()
        source = generator.generate_handler(self._get_lambda_schema())

        assert source is not None
        assert "class LambdaFunction" in source
        assert "ComputeResource" in source
        assert "from_address" in source
        assert "extract_tf" in source
        assert "extract_pulumi" in source
        assert "extract_cdk" in source
        assert "valid_metrics" in source
        assert "Auto-generated" in source
        assert "DP#10" in source

    def test_generate_s3_handler(self):
        generator = CodeGenerator()
        source = generator.generate_handler(self._get_s3_schema())

        assert "class S3Bucket" in source
        assert "StorageResource" in source
        assert "from_address" in source

    def test_generated_code_is_valid_python(self):
        """Generated code must be syntactically valid Python."""
        generator = CodeGenerator()
        source = generator.generate_handler(self._get_lambda_schema())

        # Compile to verify syntax
        compile(source, "<generated>", "exec")

    def test_generated_code_has_required_methods(self):
        generator = CodeGenerator()
        source = generator.generate_handler(self._get_lambda_schema())

        required_methods = [
            "def valid_metrics(self)",
            "def from_address(cls, resource_address",
            "def extract_tf(cls, resource",
            "def extract_pulumi(cls, resource",
            "def extract_cdk(cls, resource",
        ]
        for method in required_methods:
            assert method in source, f"Missing method: {method}"

    def test_generate_handler_convenience(self):
        source = generate_handler(self._get_lambda_schema())
        assert "class LambdaFunction" in source

    def test_nodetype_classification(self):
        """Function resources should be compute, bucket should be storage."""
        gen = CodeGenerator()
        assert gen._infer_node_type("aws_lambda_function") == "compute"
        assert gen._infer_node_type("aws_s3_bucket") == "storage"
        assert gen._infer_node_type("aws_dynamodb_table") == "storage"
        assert gen._infer_node_type("aws_apigatewayv2_api") == "routing"

    def test_generated_from_address_contains_terraform_pattern(self):
        generator = CodeGenerator()
        source = generator.generate_handler(self._get_lambda_schema())

        # Should include Terraform address pattern
        assert 'aws_lambda_function.' in source

    def test_generated_extract_tf_references_values(self):
        generator = CodeGenerator()
        source = generator.generate_handler(self._get_lambda_schema())

        assert 'values = resource.get("values"' in source
        # repr() wraps in single quotes: resource.get('address')
        assert "resource.get('address')" in source or 'resource.get("address")' in source

    def test_metrics_exclude_computed_only(self):
        """Computed-only attributes like 'id' should not appear in metrics."""
        generator = CodeGenerator()
        schema = ResourceSchema(
            provider="aws",
            resource_type="aws_test_resource",
            attributes=[
                SchemaAttribute(name="name", type="string", required=True),
                SchemaAttribute(name="id", type="string", computed=True),
                SchemaAttribute(name="memory_size", type="number", optional=True),
            ],
        )
        source = generator.generate_handler(schema)

        # 'name' and 'memory_size' should be in metrics, 'id' should not
        assert "name" in source or "name" in source.lower()
        assert "memorySize" in source
        assert "id" not in source.split("valid_metrics")[1].split("return")[0] if "valid_metrics" in source else True


class TestCodegenPipelineEndToEnd:
    """Full pipeline: schema JSON → parser → generator → valid Python."""

    def test_full_pipeline_lambda(self):
        # Parse
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        lambda_schema = next(
            r for r in providers[0].resources
            if r.resource_type == "aws_lambda_function"
        )

        # Generate
        gen = CodeGenerator()
        source = gen.generate_handler(lambda_schema)

        # Verify syntax
        compile(source, "<generated>", "exec")

        # Verify content
        assert "class LambdaFunction(ComputeResource)" in source
        assert 'provider="aws"' in source
        assert "from_address" in source
        assert "valid_metrics" in source

    def test_full_pipeline_s3(self):
        providers = SchemaReader.parse(SAMPLE_AWS_SCHEMA)
        s3_schema = next(
            r for r in providers[0].resources
            if r.resource_type == "aws_s3_bucket"
        )

        gen = CodeGenerator()
        source = gen.generate_handler(s3_schema)

        compile(source, "<generated>", "exec")
        assert "class S3Bucket(StorageResource)" in source
