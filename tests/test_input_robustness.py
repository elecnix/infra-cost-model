"""A malformed or non-finite input stops the run with one error line.

Every command reads files a user didn't write by hand: models, IaC exports,
Infracost breakdowns and Cost Explorer payloads. A NaN, an infinity or a
negative quantity used to price into a total that `--budget` and
`--fail-on-drift` read as a pass, and a file of the wrong shape crashed with a
traceback. These tests pin the refusal for each.
"""

import json
from pathlib import Path

import pytest
import yaml

from infra_cost_model.cli import main

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "serverless-api.yaml"


def example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text())


def write_model(tmp_path, data, name="model.yaml") -> str:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
    return str(path)


def first_metric(data) -> dict:
    node = next(n for n in data["nodes"].values() if n.get("usageMetrics"))
    return next(iter(node["usageMetrics"].values()))


def one_error_line(capsys) -> str:
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "Error" in err, err
    return err


# --- Non-finite numbers ----------------------------------------------------------


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_frequency_fails_the_budget(tmp_path, capsys, value):
    data = example()
    data["workflow"]["frequency"]["value"] = value
    path = write_model(tmp_path, data)
    assert main(["compute", path, "--time-basis", "monthly", "--pricing", "seed",
                 "--budget", "10"]) == 1
    one_error_line(capsys)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_metric_value_is_refused(tmp_path, capsys, value):
    data = example()
    first_metric(data)["value"] = value
    path = write_model(tmp_path, data)
    assert main(["compute", path, "--pricing", "seed", "--format", "json"]) == 1
    one_error_line(capsys)


def test_a_non_finite_bill_amount_is_unreadable(tmp_path, capsys):
    actuals = tmp_path / "actuals.json"
    actuals.write_text(json.dumps({"ResultsByTime": [{
        "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
        "Groups": [{"Keys": ["Amazon DynamoDB"],
                    "Metrics": {"UnblendedCost": {"Amount": "NaN", "Unit": "USD"}}}]}]}))
    assert main(["reconcile", str(EXAMPLE), "--actuals", str(actuals), "--pricing", "seed",
                 "--window-days", "1", "--fail-on-drift"]) == 1


# --- Negative quantities ---------------------------------------------------------


def test_compute_refuses_what_validate_refuses(tmp_path, capsys):
    data = example()
    data["workflow"]["frequency"]["value"] = -1000
    path = write_model(tmp_path, data)
    assert main(["validate", path]) == 1
    capsys.readouterr()
    assert main(["compute", path, "--pricing", "seed"]) == 1
    one_error_line(capsys)


def test_a_negative_metric_value_is_invalid(tmp_path, capsys):
    data = example()
    first_metric(data)["value"] = -512
    path = write_model(tmp_path, data)
    assert main(["validate", path]) == 1


# --- Malformed models -------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "a: [",
    "workflow: {name: w, entry: a, frequency: {unit: perMonth, value: lots}}\n"
    "nodes: {a: {nodeType: compute}}\nedges: []\n",
    "workflow: {name: w, entry: a, frequency: {unit: perMonth, value: 1}}\n"
    "nodes: {a: {nodeType: compute}}\nedges: 5\n",
])
def test_a_malformed_model_is_one_error_line(tmp_path, capsys, text):
    path = write_model(tmp_path, text)
    assert main(["compute", path, "--pricing", "seed"]) == 1
    one_error_line(capsys)


@pytest.mark.parametrize("value", [None, [1], "9" * 400])
def test_a_metric_value_that_is_not_a_quantity_is_refused(tmp_path, capsys, value):
    data = example()
    first_metric(data)["value"] = value
    path = write_model(tmp_path, data)
    assert main(["compute", path, "--pricing", "seed"]) == 1
    one_error_line(capsys)


# --- Malformed IaC and Infracost files --------------------------------------------


@pytest.mark.parametrize("fmt,payload", [
    *[(fmt, payload) for fmt in ("terraform", "pulumi", "cdk", "arm")
      for payload in ("[]", "5")],
    ("terraform", '{"values": 5}'),
])
def test_extract_refuses_a_file_of_the_wrong_shape(tmp_path, capsys, fmt, payload):
    path = tmp_path / "in.json"
    path.write_text(payload)
    assert main(["extract", str(path), "--from", fmt]) == 1
    one_error_line(capsys)


@pytest.mark.parametrize("payload", ["[]", '{"projects": 5}',
                                     '{"projects": [{"breakdown": {"resources": [5]}}]}'])
def test_import_infracost_refuses_a_file_of_the_wrong_shape(tmp_path, capsys, payload):
    path = tmp_path / "in.json"
    path.write_text(payload)
    assert main(["import-infracost", str(path)]) == 1
    one_error_line(capsys)


@pytest.mark.parametrize("command", [["extract"], ["import-infracost"]])
def test_a_file_that_is_not_utf8_is_one_error_line(tmp_path, capsys, command):
    path = tmp_path / "in.json"
    path.write_bytes(b"\xff\xfe\x00{")
    assert main(command + [str(path)]) == 1
    one_error_line(capsys)


@pytest.mark.parametrize("cost", ["-50", "NaN", "abc"])
def test_import_infracost_drops_a_cost_that_is_not_a_price_and_says_so(tmp_path, capsys,
                                                                       cost):
    path = tmp_path / "in.json"
    path.write_text(json.dumps({"projects": [{"breakdown": {"resources": [
        {"name": "aws_instance.web", "resourceType": "aws_instance",
         "costComponents": [{"name": "Instance usage", "monthlyCost": cost}]}]}}]}))
    with pytest.warns(UserWarning, match="aws_instance.web"):
        main(["import-infracost", str(path)])
    assert "-50" not in capsys.readouterr().out


# --- Terraform modules ------------------------------------------------------------


def test_extract_reads_the_resources_of_child_modules(tmp_path, capsys):
    bucket = {"address": "module.store.aws_s3_bucket.data", "type": "aws_s3_bucket",
              "name": "data",
              "values": {"bucket": "data", "region": "us-east-1"}}
    plan = {"values": {"root_module": {"resources": [], "child_modules": [
        {"address": "module.store", "resources": [bucket], "child_modules": []}]}}}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert main(["extract", str(path), "--from", "terraform", "--json"]) == 0
    assert "module.store.aws_s3_bucket.data" in capsys.readouterr().out


@pytest.mark.parametrize("command", [["compute"], ["validate"], ["extract"]])
def test_a_directory_is_one_error_line(tmp_path, capsys, command):
    assert main(command + [str(tmp_path)]) == 1
    one_error_line(capsys)


def test_a_terraform_data_source_is_not_a_cost_node(tmp_path, capsys):
    """`data.` blocks look up resources that exist; they bill nothing."""
    data_source = {"address": "data.aws_s3_bucket.existing", "mode": "data",
                   "type": "aws_s3_bucket", "name": "existing", "values": {}}
    module_data = {"address": "module.m.data.aws_lambda_function.f", "mode": "data",
                   "type": "aws_lambda_function", "name": "f", "values": {}}
    plan = {"values": {"root_module": {"resources": [data_source], "child_modules": [
        {"address": "module.m", "resources": [module_data]}]}}}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    assert main(["extract", str(path), "--from", "terraform", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_a_frequency_must_be_a_number(tmp_path, capsys):
    data = example()
    data["workflow"]["frequency"]["value"] = "users"
    data["workflow"]["parameters"] = {"users": 10}
    path = write_model(tmp_path, data)
    assert main(["compute", path, "--pricing", "seed"]) == 1
    one_error_line(capsys)


@pytest.mark.parametrize("stored", ["21", "days", True, -30])
def test_early_delete_days_must_be_a_count(stored):
    from infra_cost_model.resources.azure import blob_early_delete_months
    with pytest.raises(ValueError, match="earlyDeleteDaysStored"):
        blob_early_delete_months("Cool", {"earlyDeleteDaysStored": stored})
