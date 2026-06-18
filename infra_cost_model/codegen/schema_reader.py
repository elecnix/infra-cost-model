"""Read and parse terraform provider schema JSON.

The `terraform providers schema -json` command outputs a JSON document with
this structure:

{
  "format_version": "1.0",
  "provider_schemas": {
    "registry.terraform.io/hashicorp/aws": {
      "resource_schemas": {
        "aws_lambda_function": {
          "version": 0,
          "block": {
            "attributes": {
              "memory_size": { "type": "number", ... },
              "runtime": { "type": "string", ... },
              ...
            },
            "block_types": { ... }
          }
        },
        ...
      }
    }
  }
}

This module extracts resource type definitions suitable for code generation.
"""

import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SchemaAttribute:
    """A single attribute from a terraform resource schema."""
    name: str
    type: str  # "string", "number", "bool", list/map/set
    required: bool = False
    optional: bool = False
    computed: bool = False
    description: str = ""
    sensitive: bool = False


@dataclass
class ResourceSchema:
    """Parsed terraform resource type schema."""
    provider: str  # e.g., "aws"
    resource_type: str  # e.g., "aws_lambda_function"
    attributes: list[SchemaAttribute] = field(default_factory=list)
    description: str = ""
    version: int = 0


@dataclass
class ProviderSchema:
    """Top-level provider schema containing resource definitions."""
    provider_name: str  # e.g., "registry.terraform.io/hashicorp/aws"
    resources: list[ResourceSchema] = field(default_factory=list)


class SchemaReader:
    """Reads terraform provider schema JSON and extracts resource type definitions.

    Usage:
        reader = SchemaReader()
        provider = reader.parse_file("aws-schema.json")
        for resource in provider.resources:
            print(resource.resource_type)
    """

    @staticmethod
    def parse_file(path: str) -> list[ProviderSchema]:
        """Parse a terraform providers schema JSON file.

        Args:
            path: Path to the schema JSON file (output of
                  `terraform providers schema -json`)

        Returns:
            List of ProviderSchema objects, one per provider in the file.
        """
        with open(path) as f:
            data = json.load(f)
        return SchemaReader.parse(data)

    @staticmethod
    def parse(data: dict) -> list[ProviderSchema]:
        """Parse a terraform providers schema dict.

        Args:
            data: Parsed JSON dict from terraform providers schema output.

        Returns:
            List of ProviderSchema objects.
        """
        providers = []
        provider_schemas = data.get("provider_schemas", {})

        for provider_key, provider_data in provider_schemas.items():
            provider = SchemaReader._parse_provider(provider_key, provider_data)
            if provider:
                providers.append(provider)

        return providers

    @staticmethod
    def _parse_provider(provider_key: str, provider_data: dict) -> Optional[ProviderSchema]:
        """Parse a single provider's resource schemas."""
        # Extract short provider name from registry path
        # "registry.terraform.io/hashicorp/aws" -> "aws"
        # "registry.terraform.io/hashicorp/google" -> "google"
        provider_short = provider_key.rsplit("/", 1)[-1]

        resource_schemas = provider_data.get("resource_schemas", {})
        resources = []

        for resource_type, resource_data in resource_schemas.items():
            resource = SchemaReader._parse_resource(
                provider_short, resource_type, resource_data
            )
            if resource:
                resources.append(resource)

        return ProviderSchema(
            provider_name=provider_key,
            resources=resources,
        )

    @staticmethod
    def _parse_resource(
        provider: str, resource_type: str, resource_data: dict
    ) -> Optional[ResourceSchema]:
        """Parse a single resource type definition."""
        block = resource_data.get("block", {})
        version = resource_data.get("version", 0)

        attributes = []
        for attr_name, attr_data in block.get("attributes", {}).items():
            if attr_name == "id":
                continue  # Skip computed id attribute
            attributes.append(SchemaAttribute(
                name=attr_name,
                type=SchemaReader._normalize_type(attr_data.get("type", "string")),
                required=attr_data.get("required", False),
                optional=attr_data.get("optional", False),
                computed=attr_data.get("computed", False),
                description=attr_data.get("description", ""),
                sensitive=attr_data.get("sensitive", False),
            ))

        return ResourceSchema(
            provider=provider,
            resource_type=resource_type,
            attributes=attributes,
            description=f"Auto-generated from terraform provider schema v{version}",
            version=version,
        )

    @staticmethod
    def _normalize_type(tf_type: str) -> str:
        """Normalize terraform type to a Python-compatible type name.

        Handles complex types like:
            ["list", "string"] -> "list[string]"
            ["set", "number"] -> "set[number]"
            ["map", "string"] -> "map[string]"
        """
        if isinstance(tf_type, list):
            return f"{tf_type[0]}[{tf_type[1] if len(tf_type) > 1 else 'any'}]"
        if isinstance(tf_type, str):
            return tf_type
        return "any"
