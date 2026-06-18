"""Tests for Python SDK cost model declaration."""

import pytest
import tempfile
import os
from infra_cost_model.sdk import (
    Workflow, Call, NodeUsage,
    per_minute, per_second, per_hour, per_day, per_week, per_month,
    parse_yaml_dsl,
)


def test_per_minute_frequency():
    """Test per_minute helper creates correct frequency."""
    freq = per_minute(1000)
    assert freq.value == 1000
    assert freq.unit == "perMinute"


def test_per_second_frequency():
    """Test per_second helper creates correct frequency."""
    freq = per_second(100)
    assert freq.value == 100
    assert freq.unit == "perSecond"


def test_per_hour_frequency():
    """Test per_hour helper creates correct frequency."""
    freq = per_hour(50)
    assert freq.value == 50
    assert freq.unit == "perHour"


def test_per_day_frequency():
    """Test per_day helper creates correct frequency."""
    freq = per_day(1_000_000)
    assert freq.value == 1_000_000
    assert freq.unit == "perDay"


def test_per_week_frequency():
    """Test per_week helper creates correct frequency."""
    freq = per_week(10_000)
    assert freq.value == 10_000
    assert freq.unit == "perWeek"


def test_per_month_frequency():
    """Test per_month helper creates correct frequency."""
    freq = per_month(3_000_000)
    assert freq.value == 3_000_000
    assert freq.unit == "perMonth"


def test_workflow_creation():
    """Test basic workflow creation."""
    workflow = Workflow("my-api")
    workflow.entry = "aws_api_gateway_rest_api.my_api"
    workflow.frequency = per_minute(1000)
    
    model = workflow.to_cost_model()
    
    assert model["version"] == "1.0"
    assert model["workflow"]["name"] == "my-api"
    assert model["workflow"]["entry"] == "aws_api_gateway_rest_api.my_api"
    assert model["workflow"]["frequency"]["unit"] == "perMinute"
    assert model["workflow"]["frequency"]["value"] == 1000


def test_from_tf_creation():
    """Test workflow created from Terraform state JSON file."""
    import tempfile
    
    # Create a mock terraform state JSON
    tf_state = {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_lambda_function.handler",
                        "values": {"memory_size": 256, "region": "us-east-1"}
                    },
                    {
                        "address": "aws_dynamodb_table.users",
                        "values": {"region": "us-east-1"}
                    }
                ]
            }
        }
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        import json
        json.dump(tf_state, f)
        temp_path = f.name
    
    try:
        workflow = Workflow.from_tf(
            "my-api",
            "./infra/",
            entry="aws_api_gateway_rest_api.my_api",
            frequency=per_minute(1000),
            use_state_file=temp_path,
        )
        
        assert workflow.name == "my-api"
        assert workflow.entry == "aws_api_gateway_rest_api.my_api"
        assert workflow.frequency.value == 1000
        # Resources stored in _resources (resource representation), per Principle 5
        assert "aws_lambda_function.handler" in workflow._resources
        assert "aws_dynamodb_table.users" in workflow._resources
        # assemble() joins resource representation with cost model annotations
        assembled = workflow.assemble()
        assert "aws_lambda_function.handler" in assembled
        assert "aws_dynamodb_table.users" in assembled
    finally:
        os.unlink(temp_path)


def test_calls_definition():
    """Test defining calls between nodes."""
    workflow = Workflow("test")
    workflow.entry = "api_gateway"
    workflow.frequency = per_minute(1000)
    
    workflow.calls("api_gateway", [
        Call(to="aws_lambda_function.get_user", rate=0.8),
        Call(to="aws_lambda_function.create_user", rate=0.2),
    ])
    
    model = workflow.to_cost_model()
    
    assert len(model["edges"]) == 2
    assert model["edges"][0]["from"] == "api_gateway"
    assert model["edges"][0]["to"] == "aws_lambda_function.get_user"
    assert model["edges"][0]["rate"] == 0.8


def test_calls_with_types():
    """Test calls with read/write/invoke types."""
    workflow = Workflow("test")
    workflow.entry = "handler"
    workflow.frequency = per_minute(100)
    
    workflow.calls("handler", [
        Call(to="users_table", rate=1, type="read"),
        Call(to="events_table", rate=1, type="write"),
    ])
    
    model = workflow.to_cost_model()
    
    assert model["edges"][0]["type"] == "read"
    assert model["edges"][1]["type"] == "write"


def test_usage_metrics():
    """Test setting usage metrics on nodes."""
    workflow = Workflow("test")
    workflow.entry = "handler"
    workflow.frequency = per_minute(100)
    
    workflow.usage("handler", NodeUsage().with_metric(
        "avgDurationMs", value=200, unit="ms"
    ).with_metric("memoryMb", value=256, unit="MB"))
    
    model = workflow.to_cost_model()
    
    assert "usageMetrics" in model["nodes"]["handler"]
    assert model["nodes"]["handler"]["usageMetrics"]["avgDurationMs"]["value"] == 200


def test_from_yaml_loading():
    """Test loading workflow from YAML file (standard format)."""
    yaml_content = """
version: "1.0"
workflow:
  name: "api-workflow"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 1000
nodes:
  api_gateway:
    nodeType: routing
    resourceAddress: aws_api_gateway_rest_api.test_api
edges:
  - from: api_gateway
    to: lambda_fn
    rate: 1.0
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        workflow = Workflow.from_yaml(temp_path)
        
        assert workflow.name == "api-workflow"
        assert workflow.entry == "api_gateway"
        assert workflow.frequency.value == 1000
    finally:
        os.unlink(temp_path)


def test_from_yaml_dsl_with_arrow_syntax():
    """Test loading workflow from YAML file with arrow DSL syntax."""
    yaml_content = """
workflow:
  name: "api-workflow"
  entry: "aws_api_gateway_rest_api.my_api"
  frequency:
    unit: perMinute
    value: 1000

calls:
  aws_api_gateway_rest_api.my_api:
    data_out: 50KB
    → aws_lambda_function.get_user: 0.8
    → aws_lambda_function.create_user: 0.2

  aws_lambda_function.get_user:
    compute: 200ms
    memory: 256MB
    → aws_dynamodb_table.users:
        rate: 1
        type: read

nodes:
  aws_api_gateway_rest_api.my_api:
    nodeType: routing
    resourceAddress: aws_api_gateway_rest_api.my_api
  aws_lambda_function.get_user:
    nodeType: compute
    resourceAddress: aws_lambda_function.get_user
  aws_lambda_function.create_user:
    nodeType: compute
    resourceAddress: aws_lambda_function.create_user
  aws_dynamodb_table.users:
    nodeType: storage
    resourceAddress: aws_dynamodb_table.users
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        workflow = Workflow.from_yaml(temp_path)
        
        assert workflow.entry == "aws_api_gateway_rest_api.my_api"
        assert len(workflow._edges) == 3
        assert workflow._edges[0]["from"] == "aws_api_gateway_rest_api.my_api"
        assert workflow._edges[0]["to"] == "aws_lambda_function.get_user"
        assert workflow._edges[0]["rate"] == 0.8
        assert workflow._edges[1]["to"] == "aws_lambda_function.create_user"
        assert workflow._edges[1]["rate"] == 0.2
        assert workflow._edges[2]["to"] == "aws_dynamodb_table.users"
        assert workflow._edges[2]["type"] == "read"
    finally:
        os.unlink(temp_path)


def test_from_yaml_dsl_with_ascii_arrow_syntax():
    """Test loading workflow from YAML file with ASCII arrow DSL syntax."""
    yaml_content = """
workflow:
  name: "api-workflow"
  entry: "aws_api_gateway_rest_api.my_api"
  frequency:
    unit: perMinute
    value: 1000

calls:
  aws_api_gateway_rest_api.my_api:
    data_out: 50KB
    -> aws_lambda_function.get_user: 0.8
    -> aws_lambda_function.create_user: 0.2

  aws_lambda_function.get_user:
    compute: 200ms
    memory: 256MB
    -> aws_dynamodb_table.users:
        rate: 1
        type: read

nodes:
  aws_api_gateway_rest_api.my_api:
    nodeType: routing
    resourceAddress: aws_api_gateway_rest_api.my_api
  aws_lambda_function.get_user:
    nodeType: compute
    resourceAddress: aws_lambda_function.get_user
  aws_lambda_function.create_user:
    nodeType: compute
    resourceAddress: aws_lambda_function.create_user
  aws_dynamodb_table.users:
    nodeType: storage
    resourceAddress: aws_dynamodb_table.users
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        f.write(yaml_content)
        temp_path = f.name
    
    try:
        workflow = Workflow.from_yaml(temp_path)
        
        assert workflow.entry == "aws_api_gateway_rest_api.my_api"
        assert len(workflow._edges) == 3
        assert workflow._edges[0]["from"] == "aws_api_gateway_rest_api.my_api"
        assert workflow._edges[0]["to"] == "aws_lambda_function.get_user"
        assert workflow._edges[0]["rate"] == 0.8
        assert workflow._edges[1]["to"] == "aws_lambda_function.create_user"
        assert workflow._edges[1]["rate"] == 0.2
        assert workflow._edges[2]["to"] == "aws_dynamodb_table.users"
        assert workflow._edges[2]["type"] == "read"
    finally:
        os.unlink(temp_path)


def test_parse_yaml_dsl_shorthand_frequency():
    """Test parsing shorthand frequency notation."""
    yaml_content = """
workflow:
  name: "test"
  entry: "api_gateway"
  frequency: "1000/min"

calls:
  api_gateway:
    → lambda_fn: 1
"""
    
    model = parse_yaml_dsl(yaml_content)
    
    assert model["workflow"]["frequency"]["unit"] == "perMinute"
    assert model["workflow"]["frequency"]["value"] == 1000
    assert len(model["edges"]) == 1
    assert model["edges"][0]["to"] == "lambda_fn"


def test_parse_yaml_dsl_shorthand_frequency_week():
    """Test parsing shorthand frequency notation with /week."""
    yaml_content = """
workflow:
  name: "test"
  entry: "api_gateway"
  frequency: "50000/week"

calls:
  api_gateway:
    → lambda_fn: 1
"""
    model = parse_yaml_dsl(yaml_content)
    assert model["workflow"]["frequency"]["unit"] == "perWeek"
    assert model["workflow"]["frequency"]["value"] == 50000


def test_parse_yaml_dsl_shorthand_frequency_month():
    """Test parsing shorthand frequency notation with /month."""
    yaml_content = """
workflow:
  name: "test"
  entry: "api_gateway"
  frequency: "3000000/month"

calls:
  api_gateway:
    → lambda_fn: 1
"""
    model = parse_yaml_dsl(yaml_content)
    assert model["workflow"]["frequency"]["unit"] == "perMonth"
    assert model["workflow"]["frequency"]["value"] == 3000000


def test_parse_yaml_dsl_with_edge_config():
    """Test parsing arrow syntax with edge configuration."""
    yaml_content = """
workflow:
  name: "test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 100

calls:
  api_gateway:
    → aws_dynamodb_table.users:
        rate: 1
        type: read
"""
    
    model = parse_yaml_dsl(yaml_content)
    
    assert len(model["edges"]) == 1
    assert model["edges"][0]["type"] == "read"
    assert model["edges"][0]["rate"] == 1


def test_parse_yaml_dsl_with_ascii_arrow_edge_config():
    """Test parsing ASCII arrow syntax with edge configuration."""
    yaml_content = """
workflow:
  name: "test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 100

calls:
  api_gateway:
    -> aws_dynamodb_table.users:
        rate: 1
        type: read
"""
    
    model = parse_yaml_dsl(yaml_content)
    
    assert len(model["edges"]) == 1
    assert model["edges"][0]["type"] == "read"
    assert model["edges"][0]["rate"] == 1
    assert model["edges"][0]["from"] == "api_gateway"
    assert model["edges"][0]["to"] == "aws_dynamodb_table.users"


def test_parse_yaml_dsl_mixed_arrows():
    """Test that Unicode and ASCII arrows can be mixed in the same DSL."""
    yaml_content = """
workflow:
  name: "test"
  entry: "api_gateway"
  frequency:
    unit: perMinute
    value: 100

calls:
  api_gateway:
    → lambda_fn: 0.8
    -> dynamo_db: 0.2
"""
    
    model = parse_yaml_dsl(yaml_content)
    
    assert len(model["edges"]) == 2
    assert model["edges"][0]["to"] == "lambda_fn"
    assert model["edges"][0]["rate"] == 0.8
    assert model["edges"][1]["to"] == "dynamo_db"
    assert model["edges"][1]["rate"] == 0.2


def test_validate_valid_workflow():
    """Test validation of a valid workflow."""
    workflow = Workflow("test")
    workflow.entry = "api_gateway"
    workflow.frequency = per_minute(1000)
    
    errors = workflow.validate()
    assert errors == []


def test_validate_invalid_missing_entry():
    """Test validation of workflow without required fields."""
    workflow = Workflow("test")
    workflow.entry = "api_gateway"
    workflow.frequency = per_minute(1000)
    
    # Schema validates structure, not semantic correctness
    # Entry node existence checking is done by engine, not schema
    errors = workflow.validate()
    assert errors == []


def test_call_with_data_size():
    """Test call with data size definition."""
    workflow = Workflow("test")
    workflow.entry = "api_gateway"
    workflow.frequency = per_minute(100)
    
    workflow.calls("api_gateway", [
        Call(to="lambda_fn", rate=1, data_size={"unit": "kB", "average": 50}),
    ])
    
    model = workflow.to_cost_model()
    
    assert "dataSize" in model["edges"][0]
    assert model["edges"][0]["dataSize"]["average"] == 50


def test_percentage_pricing_node():
    """Test node with percentage-based pricing (e.g., Stripe)."""
    workflow = Workflow("test")
    workflow.entry = "api_gateway"
    workflow.frequency = per_minute(100)
    
    workflow._nodes["stripe"] = {
        "nodeType": "external",
        "resourceAddress": "external.stripe_payments",
        "pricingModel": "percentage",
        "pricingRates": {
            "percentageRate": 0.029,
            "fixedPerTransaction": 0.30,
        },
        "usageMetrics": {
            "transactionVolume": {"value": 10000, "unit": "USD"},
            "invocations": {"value": 1, "unit": "requests"},
        },
    }
    
    model = workflow.to_cost_model()
    
    assert model["nodes"]["stripe"]["pricingModel"] == "percentage"


def test_nodes_auto_extracted_from_tf_state():
    """Test that nodes are automatically extracted from Terraform state JSON."""
    import tempfile
    import json
    
    tf_state = {
        "values": {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_lambda_function.handler",
                        "values": {"memory_size": 256, "region": "us-east-1"}
                    },
                    {
                        "address": "aws_dynamodb_table.users",
                        "values": {"region": "us-east-1"}
                    }
                ]
            }
        }
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(tf_state, f)
        temp_path = f.name
    
    try:
        workflow = Workflow.from_tf(
            "my-api",
            "./infra/",
            entry="aws_api_gatewayv2_api.my_api",
            frequency=per_minute(1000),
            use_state_file=temp_path,
        )
        
        # Resources stored in _resources (resource representation), per Principle 5
        assert "aws_lambda_function.handler" in workflow._resources
        assert workflow._resources["aws_lambda_function.handler"]["nodeType"] == "compute"
        assert workflow._resources["aws_lambda_function.handler"]["provider"] == "aws"
        
        assert "aws_dynamodb_table.users" in workflow._resources
        assert workflow._resources["aws_dynamodb_table.users"]["nodeType"] == "storage"
    finally:
        os.unlink(temp_path)

class TestWorkflowParameters:
    """Tests for DP#4: symbolic parameters in Workflow SDK."""

    def test_parameter_fluent_api(self):
        """Workflow.parameter() sets symbolic parameters via fluent API."""
        workflow = Workflow("test")
        workflow.entry = "api_gateway"
        workflow.frequency = per_minute(100)

        workflow.parameter("cache_hit_rate", 0.8)
        workflow.parameter("average_payload_size", 50.0)

        assert workflow.parameters["cache_hit_rate"] == 0.8
        assert workflow.parameters["average_payload_size"] == 50.0

    def test_parameter_export_to_cost_model(self):
        """Parameters are exported in to_cost_model()."""
        workflow = Workflow("test")
        workflow.entry = "api_gateway"
        workflow.frequency = per_minute(100)
        workflow.parameter("cache_hit_rate", 0.8)

        model = workflow.to_cost_model()

        assert "parameters" in model["workflow"]
        assert model["workflow"]["parameters"]["cache_hit_rate"] == 0.8

    def test_parameter_omitted_when_empty(self):
        """Parameters key is omitted from cost model when no parameters set."""
        workflow = Workflow("test")
        workflow.entry = "api_gateway"
        workflow.frequency = per_minute(100)

        model = workflow.to_cost_model()

        # Parameters key should not be present when empty
        assert "parameters" not in model["workflow"]

    def test_parameter_import_from_yaml(self):
        """Parameters are imported from YAML workflow.parameters."""
        yaml_content = """
workflow:
  name: "test"
  entry: "api_gateway"
  frequency:
    value: 100
    unit: perMinute
  parameters:
    cache_hit_rate: 0.75
    traffic_multiplier: 2.0
edges: []
nodes: {}
"""
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            workflow = Workflow.from_yaml(temp_path)
            assert workflow.parameters["cache_hit_rate"] == 0.75
            assert workflow.parameters["traffic_multiplier"] == 2.0
        finally:
            os.unlink(temp_path)

    def test_parameter_chaining(self):
        """Parameter() returns self for fluent chaining."""
        workflow = Workflow("test")
        workflow.entry = "api_gateway"
        workflow.frequency = per_minute(100)

        result = workflow.parameter("x", 1.0).parameter("y", 2.0)
        assert result is workflow
        assert workflow.parameters["x"] == 1.0
        assert workflow.parameters["y"] == 2.0


class TestIaCExtraction:
    """Tests for Pulumi and CDK code generation (DP#10)."""

    def test_from_pulumi_with_json_path(self):
        """Workflow.from_pulumi() extracts resources from stack export JSON."""
        import tempfile
        import json
        import os

        pulumi_json = {
            "deployment": {
                "resources": [
                    {
                        "id": "aws:lambda:Function:myHandler",
                        "type": "aws:lambda/function:Function",
                        "inputs": {"memorySize": 256, "timeout": 30},
                    },
                    {
                        "id": "aws:dynamodb:Table:myTable",
                        "type": "aws:dynamodb/table:Table",
                        "inputs": {"billingMode": "PAY_PER_REQUEST"},
                    },
                ]
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(pulumi_json, f)
            temp_path = f.name

        try:
            workflow = Workflow.from_pulumi(
                "my-api",
                entry="aws:lambda:Function:myHandler",
                frequency=per_minute(100),
                json_path=temp_path,
            )

            assert "aws:lambda:Function:myHandler" in workflow._resources
            assert workflow._resources["aws:lambda:Function:myHandler"]["nodeType"] == "compute"
            assert "aws:dynamodb:Table:myTable" in workflow._resources
            assert workflow._resources["aws:dynamodb:Table:myTable"]["nodeType"] == "storage"
        finally:
            os.unlink(temp_path)

    def test_from_cdk_with_json_path(self):
        """Workflow.from_cdk() extracts resources from CDK template JSON."""
        import tempfile
        import json
        import os

        cdk_json = {
            "Resources": {
                "MyFunction": {
                    "Type": "AWS::Lambda::Function",
                    "Properties": {
                        "Handler": "index.handler",
                        "Runtime": "python3.9",
                        "MemorySize": 256,
                    },
                },
                "MyTable": {
                    "Type": "AWS::DynamoDB::Table",
                    "Properties": {
                        "BillingMode": "PAY_PER_REQUEST",
                    },
                },
            }
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(cdk_json, f)
            temp_path = f.name

        try:
            workflow = Workflow.from_cdk(
                "my-api",
                entry="AWS::Lambda::Function:MyFunction",
                frequency=per_minute(100),
                json_path=temp_path,
            )

            # Resources stored in _resources (resource representation), per Principle 5
            assert "AWS::Lambda::Function:MyFunction" in workflow._resources
            assert workflow._resources["AWS::Lambda::Function:MyFunction"]["nodeType"] == "compute"
            assert "AWS::DynamoDB::Table:MyTable" in workflow._resources
            assert workflow._resources["AWS::DynamoDB::Table:MyTable"]["nodeType"] == "storage"
        finally:
            os.unlink(temp_path)

    def test_from_pulumi_missing_file_raises(self):
        """from_pulumi with nonexistent json_path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            Workflow.from_pulumi(
                "test",
                entry="test",
                frequency=per_minute(100),
                json_path="/nonexistent/pulumi.json",
            )

    def test_from_cdk_missing_file_raises(self):
        """from_cdk with nonexistent json_path raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            Workflow.from_cdk(
                "test",
                entry="test",
                frequency=per_minute(100),
                json_path="/nonexistent/cdk.json",
            )


class TestResourceCostModelSeparation:
    """Tests for Principle 5: separate resource and cost model representations."""
    
    def test_assemble_joins_resources_and_annotations(self):
        """assemble() merges resource configs with cost model annotations."""
        workflow = Workflow("test")
        workflow.entry = "api_gw"
        workflow.frequency = per_minute(100)
        # Simulate IaC extraction
        workflow._resources["aws_lambda.handler"] = {
            "nodeType": "compute",
            "resourceAddress": "aws_lambda.handler",
            "provider": "aws",
            "service": "AWSLambda",
        }
        # Annotate with usage metrics
        workflow._nodes["aws_lambda.handler"] = {
            "usageMetrics": {"invocations": {"value": 1, "unit": "requests"}},
        }
        
        assembled = workflow.assemble()
        
        assert assembled["aws_lambda.handler"]["nodeType"] == "compute"
        assert assembled["aws_lambda.handler"]["provider"] == "aws"
        assert "usageMetrics" in assembled["aws_lambda.handler"]
    
    def test_assemble_includes_cost_model_only_nodes(self):
        """Nodes only in cost model (no IaC resource) are included."""
        workflow = Workflow("test")
        workflow.entry = "api_gw"
        workflow.frequency = per_minute(100)
        # Stripe has no IaC resource
        workflow._nodes["stripe.payments"] = {
            "nodeType": "external",
            "resourceAddress": "stripe.payments",
            "pricingModel": "percentage",
        }
        
        assembled = workflow.assemble()
        
        assert "stripe.payments" in assembled
        assert assembled["stripe.payments"]["pricingModel"] == "percentage"
    
    def test_resource_representation_property(self):
        """resource_representation returns the IaC resource dict."""
        workflow = Workflow("test")
        workflow._resources["lambda"] = {"nodeType": "compute"}
        
        rep = workflow.resource_representation
        assert "lambda" in rep
    
    def test_cost_model_annotations_property(self):
        """cost_model_annotations returns the annotation dict."""
        workflow = Workflow("test")
        workflow._nodes["lambda"] = {"usageMetrics": {}}
        
        annotations = workflow.cost_model_annotations
        assert "lambda" in annotations
    
    def test_with_resources_swaps_infrastructure(self):
        """with_resources() creates a copy with different infrastructure."""
        workflow = Workflow("test")
        workflow.entry = "api_gw"
        workflow.frequency = per_minute(100)
        workflow._resources["lambda"] = {
            "nodeType": "compute",
            "resourceAddress": "lambda",
            "provider": "aws",
        }
        workflow._nodes["lambda"] = {
            "usageMetrics": {"invocations": {"value": 1, "unit": "requests"}},
        }
        workflow._edges = [{"from": "api_gw", "to": "lambda", "rate": 1.0}]
        
        # Swap to staging resources
        staging_resources = {
            "lambda": {
                "nodeType": "compute",
                "resourceAddress": "lambda",
                "provider": "aws",
                "region": "us-west-2",  # Different region
            }
        }
        staging = workflow.with_resources(staging_resources)
        
        # Same cost model
        assert staging.entry == workflow.entry
        assert staging.frequency.value == workflow.frequency.value
        assert staging._edges == workflow._edges
        assert staging._nodes == workflow._nodes
        # Different resources
        assert staging._resources["lambda"]["region"] == "us-west-2"
        # Original unchanged
        assert "region" not in workflow._resources["lambda"]
    
    def test_with_resources_produces_different_costs(self):
        """Same cost model × different resources = different costs."""
        from infra_cost_model.engine.engine import CostEngine
        
        workflow = Workflow("test")
        workflow.entry = "api_gw"
        workflow.frequency = per_minute(100)
        workflow._resources["api_gw"] = {
            "nodeType": "routing",
            "resourceAddress": "api_gw",
            "provider": "aws",
            "service": "APIGateway",
        }
        workflow._resources["lambda"] = {
            "nodeType": "compute",
            "resourceAddress": "lambda",
            "provider": "aws",
            "service": "AWSLambda",
            "usageMetrics": {"invocations": {"value": 1, "unit": "requests"}},
            "pricingRates": {"invocations": 0.001},
        }
        workflow._edges = [{"from": "api_gw", "to": "lambda", "rate": 1.0}]
        
        cost1 = CostEngine(workflow.to_cost_model()).total_cost()
        
        # Same resources, same cost
        staging = workflow.with_resources(workflow._resources)
        cost2 = CostEngine(staging.to_cost_model()).total_cost()
        assert cost2 == pytest.approx(cost1)
    
    def test_to_cost_model_calls_assemble(self):
        """to_cost_model() uses assemble() to join representations."""
        workflow = Workflow("test")
        workflow.entry = "api_gw"
        workflow.frequency = per_minute(100)
        workflow._resources["lambda"] = {
            "nodeType": "compute",
            "resourceAddress": "lambda",
            "provider": "aws",
        }
        workflow._nodes["lambda"] = {
            "usageMetrics": {"invocations": {"value": 1, "unit": "requests"}},
        }
        
        model = workflow.to_cost_model()
        
        assert model["nodes"]["lambda"]["nodeType"] == "compute"
        assert "usageMetrics" in model["nodes"]["lambda"]