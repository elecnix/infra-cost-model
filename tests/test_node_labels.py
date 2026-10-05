"""Tests for node labels: grouping and filtering a model's costs (#445)."""

import pytest

from infra_cost_model.cli import main
from infra_cost_model.labels import (
    UNLABELED,
    excluded_addresses,
    group_nodes,
    label_keys,
    node_labels,
    parse_label_selector,
)
from infra_cost_model.schema import validate_cost_model
from infra_cost_model.sdk import parse_yaml_dsl

# Four fixed monthly costs, priced offline through pricingRates:
# api 100, llm 21, tax 50, bucket 5 — total 176.
MODEL = """
version: "1.0"
workflow:
  name: labeled-model
  entry: api
  frequency: {unit: perMinute, value: 1}
nodes:
  api:
    nodeType: routing
    resourceAddress: aws_apigateway.test
    labels: {category: platform, env: prod}
    usageMetrics: {requests: {unit: requests, value: 1, fixed: true}}
    pricingRates: {requests: 100}
  llm:
    nodeType: external
    resourceAddress: anthropic.messages
    labels: {category: llm}
    usageMetrics: {messages: {unit: messages, value: 3, fixed: true}}
    pricingRates: {messages: 7}
  tax:
    nodeType: external
    resourceAddress: aws_tax.prod
    labels: {category: tax}
    usageMetrics: {amount: {unit: usd, value: 50, fixed: true}}
    pricingRates: {amount: 1}
  bucket:
    nodeType: storage
    resourceAddress: s3.bucket
    usageMetrics: {objects: {unit: objects, value: 1, fixed: true}}
    pricingRates: {objects: 5}
"""


@pytest.fixture
def model_file(tmp_path):
    path = tmp_path / "labeled.yaml"
    path.write_text(MODEL)
    return path


def compute(model_file, *extra):
    return main(["compute", str(model_file), "--no-catalog", "--monthly", *extra])


# --- the schema field ---

def test_node_labels_validate():
    model = parse_yaml_dsl(MODEL)
    assert validate_cost_model(model) == []


def test_node_labels_survive_the_yaml_parser():
    model = parse_yaml_dsl(MODEL)
    assert model["nodes"]["api"]["labels"] == {"category": "platform", "env": "prod"}


def test_a_label_value_must_be_a_string():
    model = parse_yaml_dsl(MODEL)
    model["nodes"]["api"]["labels"] = {"category": 3}
    errors = validate_cost_model(model)
    assert any("labels" in error for error in errors)


# --- the label helpers ---

def test_node_labels_reads_the_labels_map():
    assert node_labels({"labels": {"category": "llm"}}) == {"category": "llm"}


def test_node_labels_of_an_unlabeled_node_is_empty():
    assert node_labels({"nodeType": "storage"}) == {}


def test_label_keys_lists_every_key():
    model = parse_yaml_dsl(MODEL)
    assert label_keys(model["nodes"]) == {"category", "env"}


def test_group_nodes_sums_each_label_value():
    model = parse_yaml_dsl(MODEL)
    nodes = model["nodes"]
    costs = {"api": 100.0, "llm": 21.0, "tax": 50.0, "bucket": 5.0}
    assert group_nodes(nodes, costs, "category") == [
        ("llm", 21.0, ["llm"]),
        ("platform", 100.0, ["api"]),
        ("tax", 50.0, ["tax"]),
        (UNLABELED, 5.0, ["bucket"]),
    ]


def test_group_nodes_ignores_a_costs_key_the_model_does_not_declare():
    nodes = parse_yaml_dsl(MODEL)["nodes"]
    assert group_nodes(nodes, {"api": 100.0}, "category") == [("platform", 100.0, ["api"])]


def test_excluded_addresses_matches_key_and_value():
    nodes = parse_yaml_dsl(MODEL)["nodes"]
    assert excluded_addresses(nodes, [("category", "llm")]) == {"llm"}


def test_excluded_addresses_takes_a_node_carrying_one_of_several_selectors():
    nodes = parse_yaml_dsl(MODEL)["nodes"]
    excluded = excluded_addresses(nodes, [("category", "llm"), ("env", "prod")])
    assert excluded == {"llm", "api"}


def test_excluded_addresses_keeps_a_node_without_the_label():
    nodes = parse_yaml_dsl(MODEL)["nodes"]
    assert excluded_addresses(nodes, [("category", "nope")]) == set()


def test_parse_label_selector_splits_on_the_first_equals():
    assert parse_label_selector("category=llm") == ("category", "llm")


def test_parse_label_selector_rejects_a_missing_equals():
    with pytest.raises(ValueError):
        parse_label_selector("category")


def test_parse_label_selector_rejects_an_empty_side():
    with pytest.raises(ValueError):
        parse_label_selector("=llm")
    with pytest.raises(ValueError):
        parse_label_selector("category=")


# --- compute ---

def test_compute_group_by_prints_one_subtotal_per_label_value(model_file, capsys):
    assert compute(model_file, "--group-by", "category") == 0
    out = capsys.readouterr().out
    assert "category=llm: $21.000000" in out
    assert "category=platform: $100.000000" in out
    assert "category=tax: $50.000000" in out


def test_compute_group_by_totals_the_nodes_without_the_label(model_file, capsys):
    assert compute(model_file, "--group-by", "category") == 0
    assert f"{UNLABELED}: $5.000000" in capsys.readouterr().out


def test_compute_group_by_leaves_the_total_alone(model_file, capsys):
    assert compute(model_file, "--group-by", "category") == 0
    assert "Total Monthly Cost: $176.000000" in capsys.readouterr().out


def test_compute_group_by_a_label_no_node_carries_fails(model_file, capsys):
    assert compute(model_file, "--group-by", "catgory") == 1
    err = capsys.readouterr().err
    assert "catgory" in err
    assert "category" in err  # names the keys the model does use


def test_compute_exclude_label_drops_the_node_from_the_total(model_file, capsys):
    assert compute(model_file, "--exclude-label", "category=llm") == 0
    out = capsys.readouterr().out
    assert "Total Monthly Cost: $155.000000" in out
    # Once, in the excluded section — not among the costs the total sums.
    assert out.count("llm: $21.000000") == 1


def test_compute_exclude_label_reports_the_cost_it_left_out(model_file, capsys):
    assert compute(model_file, "--exclude-label", "category=llm") == 0
    out = capsys.readouterr().out
    assert "Excluded by label category=llm" in out
    assert "$21.000000" in out


def test_compute_exclude_label_applies_the_budget_to_the_filtered_total(model_file, capsys):
    # The whole model costs 176, so this budget only passes on the filtered total.
    assert compute(model_file, "--exclude-label", "category=llm", "--budget", "160") == 0


def test_compute_exclude_label_breaches_the_budget_on_the_filtered_total(model_file, capsys):
    assert compute(model_file, "--exclude-label", "category=llm", "--budget", "150") == 1
    assert "BUDGET BREACH" in capsys.readouterr().err


def test_compute_exclude_label_needs_key_equals_value(model_file, capsys):
    assert compute(model_file, "--exclude-label", "llm") == 1
    assert "key=value" in capsys.readouterr().err


def test_compute_exclude_label_on_a_key_no_node_carries_fails(model_file, capsys):
    assert compute(model_file, "--exclude-label", "catgory=llm") == 1
    assert "catgory" in capsys.readouterr().err


def test_compute_group_by_a_model_without_labels_says_so(tmp_path, capsys):
    path = tmp_path / "plain.yaml"
    path.write_text("""
version: "1.0"
workflow:
  name: unlabeled-model
  entry: api
  frequency: {unit: perMinute, value: 1}
nodes:
  api:
    nodeType: routing
    resourceAddress: aws_apigateway.test
""")
    assert main(["compute", str(path), "--no-catalog", "--group-by", "category"]) == 1
    assert "(none)" in capsys.readouterr().err


def test_compute_several_exclude_labels_combine(model_file, capsys):
    assert compute(
        model_file, "--exclude-label", "category=llm", "--exclude-label", "category=tax"
    ) == 0
    assert "Total Monthly Cost: $105.000000" in capsys.readouterr().out


# --- analyze ---

def test_analyze_group_by_prints_the_subtotals(model_file, capsys):
    assert main(["analyze", str(model_file), "--group-by", "category"]) == 0
    out = capsys.readouterr().out
    assert "category=platform: $100.000000" in out
    assert "Total Monthly Cost: $176.000000" in out


def test_analyze_json_reports_the_groups(model_file, capsys):
    import json

    assert main(["analyze", str(model_file), "--json", "--group-by", "category"]) == 0
    output = json.loads(capsys.readouterr().out)
    groups = {entry["label"]: entry for entry in output["groups"]}
    assert groups["platform"]["subtotal"] == 100.0
    assert groups["llm"]["nodes"] == ["llm"]
    assert groups[UNLABELED]["nodes"] == ["bucket"]


def test_analyze_json_reports_the_excluded_nodes(model_file, capsys):
    import json

    assert main(["analyze", str(model_file), "--json", "--exclude-label", "category=llm"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert "llm" not in output["costs"]
    assert output["excluded_nodes"] == [{"node": "llm", "cost": 21.0}]
    assert output["total_cost"] == 155.0


def test_analyze_json_omits_the_groups_without_the_flag(model_file, capsys):
    import json

    assert main(["analyze", str(model_file), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert "groups" not in output
    assert "excluded_nodes" not in output

# --- the label flags and the diffable JSON snapshot (#443) ---

def test_compute_json_refuses_a_label_filter(model_file, capsys):
    """A filtered snapshot would not be the model's snapshot.

    `--format json` exists so two snapshots of one model differ only where the
    model did (#443). A label filter would leave the excluded node in the
    snapshot while --budget gated the total without it, so the two halves of
    one run would disagree. The combination fails instead of half-applying.
    """
    assert compute(model_file, "--format", "json", "--exclude-label", "category=llm") == 1
    assert "--exclude-label" in capsys.readouterr().err


def test_compute_json_refuses_group_by(model_file, capsys):
    assert compute(model_file, "--format", "json", "--group-by", "category") == 1
    assert "--group-by" in capsys.readouterr().err


def test_compute_json_still_runs_without_the_label_flags(model_file, capsys):
    import json

    assert compute(model_file, "--format", "json") == 0
    assert json.loads(capsys.readouterr().out)["total"] == 176.0


# --- label values are strings, and an empty model has no labels to name ---

EMPTY_MODEL = """
version: "1.0"
workflow:
  name: empty
  entry: api
  frequency: {unit: perMinute, value: 1}
nodes: {}
"""


def test_compute_group_by_on_an_empty_model_reports_the_missing_entry_node(tmp_path, capsys):
    """The label check must not hide the real reason an empty model fails.

    A model with no nodes carries no labels, so every label key is "unknown"
    and the typo message would point at a label instead of at the empty model
    the engine actually rejects.
    """
    path = tmp_path / "empty.yaml"
    path.write_text(EMPTY_MODEL)
    assert main(["compute", str(path), "--no-catalog", "--group-by", "category"]) == 1
    err = capsys.readouterr().err
    assert "Entry node" in err
    assert "carries the label" not in err


def test_compute_exclude_label_on_an_empty_model_reports_the_missing_entry_node(tmp_path, capsys):
    path = tmp_path / "empty.yaml"
    path.write_text(EMPTY_MODEL)
    assert main(["compute", str(path), "--no-catalog", "--exclude-label", "category=llm"]) == 1
    err = capsys.readouterr().err
    assert "Entry node" in err
    assert "carries the label" not in err


@pytest.fixture
def int_label_file(tmp_path):
    path = tmp_path / "int-label.yaml"
    path.write_text(MODEL.replace("labels: {category: llm}", "labels: {category: 3}"))
    return path


def test_compute_group_by_rejects_a_non_string_label_value(int_label_file, capsys):
    """A label value the schema calls a string must not be grouped as anything else.

    `compute` reads labels from the parsed YAML without running `validate`, so
    the string rule has to hold here too: an integer value used to crash the
    grouping with a TypeError traceback.
    """
    assert compute(int_label_file, "--group-by", "category") == 1
    err = capsys.readouterr().err
    assert "category" in err and "string" in err
    assert "Traceback" not in err


def test_compute_exclude_label_rejects_a_non_string_label_value(int_label_file, capsys):
    assert compute(int_label_file, "--exclude-label", "category=3") == 1
    err = capsys.readouterr().err
    assert "category" in err and "string" in err
    assert "Traceback" not in err


def test_analyze_rejects_a_non_string_label_value(int_label_file, capsys):
    assert main(["analyze", str(int_label_file), "--group-by", "category"]) == 1
    err = capsys.readouterr().err
    assert "category" in err and "string" in err
    assert "Traceback" not in err


def test_compute_without_label_flags_still_runs_on_a_non_string_label(int_label_file, capsys):
    """Only the label flags read labels, so only they enforce the string rule.

    `compute` stays lenient about a model that does not satisfy the schema,
    as it already is about a missing `provider`.
    """
    assert compute(int_label_file) == 0
    assert "Total Monthly Cost:" in capsys.readouterr().out


# --- an error the model supplies must not carry control characters ---

def test_label_error_escapes_a_control_character_in_a_label_key(tmp_path, capsys):
    """A model's own label text reaches stderr, so it is escaped like a value.

    The message names the label the model got wrong, and that text comes from
    the model, so a raw escape sequence in it would reach the terminal.
    """
    path = tmp_path / "esc.yaml"
    path.write_text(MODEL.replace("labels: {category: llm}", 'labels: {"cat\\u001b[31m": 3}'))
    assert compute(path, "--group-by", "category") == 1
    err = capsys.readouterr().err
    assert "\\x1b" in err
    assert "\x1b" not in err
