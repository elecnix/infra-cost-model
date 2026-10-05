"""Bill-line identity per node and usage metric (Issue #442).

A model that prices a NAT gateway knows nothing about the two lines the
gateway's money lands on: hours bill under `Amazon Virtual Private Cloud`,
bytes processed bill under `EC2 - Other`. Without that, a user compares the
model with the bill from a mapping file kept by hand.

These tests cover the three things that make the mapping live in the model:
the known-names list every default comes from, the node-level override, and
`validate` refusing a bill name that matches zero rows.
"""

import json

import pytest

from infra_cost_model.billing import (
    billing_line_errors,
    known_line,
    resolve_billing_lines,
)
from infra_cost_model.cli import main


NAT_NODE = {
    "nodeType": "routing",
    "resourceAddress": "aws_nat_gateway.main",
    "provider": "aws",
    "service": "AmazonVPC",
    "region": "us-west-2",
    "usageMetrics": {
        "natHours": {"unit": "hours", "value": 730, "fixed": True},
        "dataProcessedGb": {"unit": "GB", "value": 0.002},
    },
}


def model_with(nodes: dict) -> dict:
    """A single-workflow model carrying ``nodes``, keyed by resource address."""
    entry = next(iter(nodes))
    return {
        "version": "1.0",
        "workflow": {
            "name": "nat-api",
            "entry": entry,
            "frequency": {"unit": "perSecond", "value": 1},
        },
        "nodes": nodes,
    }


def lines_for(node: dict) -> dict[str, tuple[str, str]]:
    """Resolve one node's lines, keyed by usage metric, as (service, usageType)."""
    resolved = resolve_billing_lines(model_with({"aws_nat_gateway.main": node}))
    return {line.metric: (line.service, line.usage_type) for line in resolved}


class TestDefaults:
    def test_each_metric_resolves_to_its_own_line(self):
        """Hours and bytes bill under different services, so the metric decides."""
        lines = lines_for(NAT_NODE)

        assert lines["natHours"] == (
            "Amazon Virtual Private Cloud",
            "USW2-NatGateway-Hours",
        )
        assert lines["dataProcessedGb"] == ("EC2 - Other", "USW2-NatGateway-Bytes")

    def test_the_region_prefix_follows_the_node(self):
        node = dict(NAT_NODE, region="us-east-1")
        lines = lines_for(node)

        assert lines["natHours"][1] == "USE1-NatGateway-Hours"

    def test_a_service_with_no_known_usage_type_resolves_to_the_service_line(self):
        """S3 bills one service under many usage types, so the service alone is
        the most a default can say. A node override names the exact line."""
        node = {
            "nodeType": "storage",
            "resourceAddress": "aws_s3_bucket.media",
            "provider": "aws",
            "service": "AmazonS3",
            "region": "us-west-2",
            "usageMetrics": {"storageGb": {"unit": "GB", "value": 100}},
        }
        lines = lines_for(node)

        assert lines["storageGb"] == ("Amazon Simple Storage Service", None)

    def test_a_metric_with_no_known_line_resolves_to_nothing(self):
        node = {
            "nodeType": "external",
            "resourceAddress": "github_seats.team",
            "provider": "github",
            "service": "Copilot",
            "region": "global",
            "usageMetrics": {"seats": {"unit": "seats", "value": 12}},
        }

        assert resolve_billing_lines(model_with({"github_seats.team": node})) == []

    def test_the_catalog_metric_decides_the_line(self):
        """`dataProcessedGb` names a different catalog metric on a NAT gateway
        than on a VPC endpoint, so the handler's mapping has to be consulted."""
        node = {
            "nodeType": "storage",
            "resourceAddress": "aws_vpc_endpoint.secrets",
            "provider": "aws",
            "service": "AmazonVPC",
            "region": "us-west-2",
            "usageMetrics": {
                "endpointHours": {"unit": "hours", "value": 730, "fixed": True},
            },
        }
        lines = lines_for(node)

        assert lines["endpointHours"][0] == "Amazon Virtual Private Cloud"

    def test_a_global_node_takes_the_global_prefix(self):
        node = {
            "nodeType": "storage",
            "resourceAddress": "aws_route53_zone.example",
            "provider": "aws",
            "service": "AmazonRoute53",
            "region": "global",
            "usageMetrics": {"hostedZones": {"unit": "zones", "value": 2}},
        }

        assert lines_for(node)["hostedZones"] == ("Amazon Route 53", None)


class TestNodeOverrides:
    def test_the_provider_defaults_to_the_vendors_bill(self):
        node = dict(NAT_NODE, billingLines={
            "natHours": {"service": "EC2 - Other"},
        })
        resolved = resolve_billing_lines(model_with({"aws_nat_gateway.main": node}))

        override = next(line for line in resolved if line.metric == "natHours")
        assert override.provider == "aws-cost-explorer"

    def test_a_node_line_wins_over_the_default(self):
        node = dict(NAT_NODE, billingLines={
            "natHours": {
                "provider": "aws-cost-explorer",
                "service": "Amazon Virtual Private Cloud",
                "usageType": "USW2-NatGateway-Hours",
            },
        })
        resolved = resolve_billing_lines(model_with({"aws_nat_gateway.main": node}))

        override = next(line for line in resolved if line.metric == "natHours")
        assert override.source == "node"
        assert override.usage_type == "USW2-NatGateway-Hours"

    def test_a_default_line_reports_where_it_came_from(self):
        resolved = resolve_billing_lines(model_with({"aws_nat_gateway.main": NAT_NODE}))

        assert {line.source for line in resolved} == {"default"}

    def test_an_override_needs_no_usage_type(self):
        node = dict(NAT_NODE, billingLines={
            "natHours": {"provider": "aws-cost-explorer", "service": "EC2 - Other"},
        })
        resolved = resolve_billing_lines(model_with({"aws_nat_gateway.main": node}))

        override = next(line for line in resolved if line.metric == "natHours")
        assert (override.service, override.usage_type) == ("EC2 - Other", None)

    def test_an_override_for_a_metric_the_node_does_not_carry_is_ignored(self):
        node = dict(NAT_NODE, billingLines={
            "natGb": {"provider": "aws-cost-explorer", "service": "EC2 - Other"},
        })
        resolved = resolve_billing_lines(model_with({"aws_nat_gateway.main": node}))

        assert "natGb" not in {line.metric for line in resolved}


class TestKnownNames:
    def test_the_bill_name_is_not_the_catalog_service_code(self):
        """The catalog says `AmazonVPC`; the bill says
        `Amazon Virtual Private Cloud`. The list carries the bill's name."""
        line = known_line("aws", "AmazonVPC", "NAT-Gateway-Hour")

        assert line["service"] == "Amazon Virtual Private Cloud"

    def test_a_catalog_service_the_bill_names_differently_is_still_known(self):
        line = known_line("aws", "AmazonECR", "ECR-Storage")

        assert line["service"] == "Amazon EC2 Container Registry (ECR)"

    def test_an_unknown_catalog_service_has_no_known_line(self):
        assert known_line("aws", "NotAService", "NotAMetric") is None


class TestValidation:
    def test_a_misspelled_service_name_is_refused(self):
        node = dict(NAT_NODE, billingLines={
            "natHours": {
                "provider": "aws-cost-explorer",
                "service": "Amazon Virtual Private Cloud ",
            },
        })
        errors = billing_line_errors(model_with({"aws_nat_gateway.main": node}))

        assert len(errors) == 1
        assert "Amazon Virtual Private Cloud " in errors[0]
        assert "natHours" in errors[0]

    def test_the_catalog_service_code_is_refused_as_a_bill_name(self):
        """The mistake the issue names: writing the code the handlers use."""
        node = dict(NAT_NODE, billingLines={
            "natHours": {"provider": "aws-cost-explorer", "service": "AmazonVPC"},
        })
        errors = billing_line_errors(model_with({"aws_nat_gateway.main": node}))

        assert len(errors) == 1
        assert "AmazonVPC" in errors[0]

    def test_an_unknown_bill_provider_is_refused(self):
        node = dict(NAT_NODE, billingLines={
            "natHours": {
                "provider": "aws-cost-explorerr",
                "service": "Amazon Virtual Private Cloud",
            },
        })
        errors = billing_line_errors(model_with({"aws_nat_gateway.main": node}))

        assert len(errors) == 1
        assert "aws-cost-explorerr" in errors[0]

    def test_a_usage_type_prefixed_for_another_region_is_refused(self):
        node = dict(NAT_NODE, billingLines={
            "natHours": {
                "provider": "aws-cost-explorer",
                "service": "Amazon Virtual Private Cloud",
                "usageType": "USE1-NatGateway-Hours",
            },
        })
        errors = billing_line_errors(model_with({"aws_nat_gateway.main": node}))

        assert len(errors) == 1
        assert "us-west-2" in errors[0]

    def test_known_names_validate(self):
        node = dict(NAT_NODE, billingLines={
            "natHours": {
                "provider": "aws-cost-explorer",
                "service": "Amazon Virtual Private Cloud",
                "usageType": "USW2-NatGateway-Hours",
            },
            "dataProcessedGb": {
                "provider": "aws-cost-explorer",
                "service": "EC2 - Other",
                "usageType": "USW2-NatGateway-Bytes",
            },
        })
        model = model_with({"aws_nat_gateway.main": node})

        assert billing_line_errors(model) == []

    def test_a_node_without_lines_is_never_an_error(self):
        assert billing_line_errors(model_with({"aws_nat_gateway.main": NAT_NODE})) == []


NAT_MODEL_YAML = """
version: "1.0"
workflow:
  name: nat-api
  entry: aws_nat_gateway.main
  frequency: 1/sec
nodes:
  aws_nat_gateway.main:
    nodeType: routing
    resourceAddress: aws_nat_gateway.main
    provider: aws
    service: AmazonVPC
    region: us-west-2
    usageMetrics:
      natHours: { unit: hours, value: 730, fixed: true }
      dataProcessedGb: { unit: GB, value: 0.002 }
"""


@pytest.fixture()
def nat_model_file(tmp_path):
    path = tmp_path / "model.yaml"
    path.write_text(NAT_MODEL_YAML)
    return path


class TestCommand:
    def test_json_prints_one_line_per_metric(self, nat_model_file, capsys):
        exit_code = main(["billing-lines", str(nat_model_file), "--json"])

        assert exit_code == 0
        output = json.loads(capsys.readouterr().out)
        by_metric = {line["metric"]: line for line in output["lines"]}
        assert by_metric["natHours"]["service"] == "Amazon Virtual Private Cloud"
        assert by_metric["natHours"]["usageType"] == "USW2-NatGateway-Hours"
        assert by_metric["dataProcessedGb"]["usageType"] == "USW2-NatGateway-Bytes"

    def test_the_table_names_the_line_of_each_metric(self, nat_model_file, capsys):
        exit_code = main(["billing-lines", str(nat_model_file)])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Amazon Virtual Private Cloud" in out
        assert "USW2-NatGateway-Bytes" in out

    def test_a_metric_with_no_known_line_is_reported_as_unmapped(
            self, nat_model_file, capsys, monkeypatch):
        from infra_cost_model.billing import lines

        monkeypatch.setattr(lines, "known_lines", lambda: {
            "aws": {"billProvider": "aws-cost-explorer", "regionPrefixes": {}, "lines": []},
        })
        exit_code = main(["billing-lines", str(nat_model_file), "--json"])

        assert exit_code == 0
        output = json.loads(capsys.readouterr().out)
        assert output["lines"] == []
        assert [entry["metric"] for entry in output["unmapped"]] == [
            "natHours", "dataProcessedGb"]

    def test_a_missing_file_is_an_error(self, tmp_path, capsys):
        exit_code = main(["billing-lines", str(tmp_path / "nope.yaml")])

        assert exit_code == 1
        assert "File not found" in capsys.readouterr().err

    def test_an_override_for_an_unknown_metric_is_refused(self, tmp_path, capsys):
        """An override for a metric the node does not carry prices nothing."""
        path = tmp_path / "model.yaml"
        path.write_text(NAT_MODEL_YAML.replace(
            "    region: us-west-2\n",
            "    region: us-west-2\n"
            "    billingLines:\n"
            "      natGb: { provider: aws-cost-explorer, service: \"EC2 - Other\","
            " usageType: \"USW2-NatGateway-Bytes\" }\n",
        ))

        exit_code = main(["validate", str(path)])

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "natGb" in out
        assert "dataProcessedGb" in out

    def test_a_misspelled_service_name_fails_validation(self, tmp_path, capsys):
        path = tmp_path / "model.yaml"
        path.write_text(NAT_MODEL_YAML.replace(
            "service: AmazonVPC\n",
            "service: AmazonVPC\n"
            "    billingLines:\n"
            "      natHours: { provider: aws-cost-explorer, service: AmazonVPC }\n",
        ))

        exit_code = main(["validate", str(path)])

        assert exit_code == 1
        assert "AmazonVPC" in capsys.readouterr().out

    def test_the_model_in_the_issue_validates(self, tmp_path, capsys):
        path = tmp_path / "model.yaml"
        path.write_text(NAT_MODEL_YAML.replace(
            "    region: us-west-2\n",
            "    region: us-west-2\n"
            "    billingLines:\n"
            "      natHours: { provider: aws-cost-explorer, service: \"Amazon Virtual Private Cloud\","
            " usageType: \"USW2-NatGateway-Hours\" }\n"
            "      dataProcessedGb: { provider: aws-cost-explorer,"
            " service: \"EC2 - Other\", usageType: \"USW2-NatGateway-Bytes\" }\n",
        ))

        assert main(["validate", str(path)]) == 0
