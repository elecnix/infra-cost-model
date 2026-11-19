"""A usage metric states DynamoDB operations, and the engine counts the units.

Before this feature, a model stated a table's read or write request units as
one number. The author applied DynamoDB's billing rule by hand: a read bills
per 4 KB block and by consistency, a write per 1 KB block and doubles in a
transaction. A metric can now state `operations`, and the DynamoDB resource
code applies the rule. From the resulting quantity on, the metric behaves as
if `value` had been that number.
"""

import json

import pytest
import yaml

from infra_cost_model.cli import main
from infra_cost_model.engine.engine import CostEngine, SensitivityAnalyzer
from infra_cost_model.pricing.catalog import PricingCatalog
from infra_cost_model.resources.dynamodb import (
    DynamoDBTable,
    OPERATION_KINDS,
    request_units,
)
from infra_cost_model.schema import validate_cost_model

TABLE = "aws_dynamodb_table.orders"


# Units arithmetic, one rule per test.

@pytest.mark.parametrize("kind,consistency,item_bytes,expected", [
    # Reads bill per 4 KB block. Eventual is half a unit, strong one, and
    # transactional two.
    ("GetItem", "eventual", 900, 0.5),
    ("GetItem", "strong", 4097, 2.0),
    ("GetItem", "strong", 4096, 1.0),
    ("GetItem", "transactional", 100, 2.0),
    # A read that finds no item still bills the minimum.
    ("GetItem", "eventual", 0, 0.5),
    ("Scan", "strong", 0, 1.0),
    # Writes bill per 1 KB block.
    ("PutItem", None, 1025, 2.0),
    ("PutItem", None, 1024, 1.0),
    ("DeleteItem", None, 0, 1.0),
    # A transaction doubles the units of a write.
    ("TransactWriteItems", None, 300, 2.0),
    ("TransactWriteItems", "transactional", 1500, 4.0),
    ("PutItem", "transactional", 300, 2.0),
    # TransactGetItems is always transactional.
    ("TransactGetItems", None, 100, 2.0),
    ("TransactGetItems", "transactional", 5000, 4.0),
])
def test_units_for_one_call(kind, consistency, item_bytes, expected):
    assert request_units(kind, consistency, item_bytes) == expected


def test_a_query_sized_by_a_parameter_bills_the_block_ceiling():
    """`orders_per_customer * 900` bytes at 10 is 9,000 bytes: 3 blocks, half each."""
    handler = DynamoDBTable()
    result = handler.operations_quantity(
        "readRequests",
        [{"kind": "Query", "itemBytes": "orders_per_customer * 900", "count": 1}],
        lambda value, what: (
            10.0 * 900 if isinstance(value, str) else float(value)),
    )
    assert result.total == 1.5


def test_every_kind_is_either_a_read_or_a_write():
    assert OPERATION_KINDS["GetItem"] == "read"
    assert OPERATION_KINDS["TransactWriteItems"] == "write"
    assert set(OPERATION_KINDS.values()) == {"read", "write"}


# A model that states operations prices to the hand-computed total.

OPERATIONS = """
version: "1.0"
workflow:
  name: orders-api
  entry: aws_dynamodb_table.orders
  frequency: { unit: perMonth, value: 1000000 }
  parameters:
    orders_per_customer: 10
    writes_per_request: 2
nodes:
  aws_dynamodb_table.orders:
    nodeType: storage
    resourceAddress: aws_dynamodb_table.orders
    provider: aws
    service: AmazonDynamoDB
    region: us-east-1
    usageMetrics:
      readRequests:
        unit: ReadRequestUnits
        operations:
          - { name: get-order,   kind: GetItem, itemBytes: 900, count: 1 }
          - { name: list-orders, kind: Query, consistency: strong, itemBytes: "orders_per_customer * 900", count: 0.2 }
      writeRequests:
        unit: WriteRequestUnits
        operations:
          - { name: put-order, kind: PutItem, itemBytes: 900, count: "writes_per_request" }
          - { name: settle,    kind: TransactWriteItems, itemBytes: 300, count: 3 }
    pricingRates:
      readRequests: 0.000000125
      writeRequests: 0.000000625
edges: []
"""

# Per invocation: get-order is 0.5, list-orders is 3 blocks x 1 x 0.2 = 0.6,
# so 1.1 read units. put-order is 1 x 2 = 2, settle is 1 x 2 x 3 = 6, so 8
# write units. A million invocations a month bill 1.1M read units at
# $0.125 per million and 8M write units at $0.625 per million.
HAND_COMPUTED_TOTAL = 1_100_000 * 0.000000125 + 8_000_000 * 0.000000625


def _model(text=OPERATIONS) -> dict:
    return yaml.safe_load(text)


def _metrics(model) -> dict:
    return model["nodes"][TABLE]["usageMetrics"]


def test_the_example_model_is_valid():
    assert validate_cost_model(_model()) == []


def test_the_example_model_prices_to_the_hand_computed_total():
    engine = CostEngine(_model(), catalog=None, time_basis="monthly")
    assert engine.total_cost() == pytest.approx(HAND_COMPUTED_TOTAL)
    assert HAND_COMPUTED_TOTAL == pytest.approx(5.1375)


def _with_values(read_units: float, write_units: float, **extra) -> dict:
    """The same model with `value` metrics holding the quantities by hand."""
    model = _model()
    metrics = _metrics(model)
    metrics["readRequests"] = {"unit": "ReadRequestUnits", "value": read_units,
                               **extra}
    metrics["writeRequests"] = {"unit": "WriteRequestUnits", "value": write_units,
                                **extra}
    return model


@pytest.mark.parametrize("extra", [{}, {"fixed": True}])
def test_the_metric_behaves_as_if_value_were_the_derived_number(extra):
    """Frequency scaling and `fixed: true` act on the derived quantity."""
    stated = _model()
    for metric in _metrics(stated).values():
        metric.update(extra)
    by_hand = _with_values(1.1, 8.0, **extra)
    assert (CostEngine(stated, None, time_basis="monthly").total_cost()
            == pytest.approx(CostEngine(by_hand, None, time_basis="monthly").total_cost()))


def test_the_catalog_prices_the_derived_quantity():
    catalog = PricingCatalog(seed=True)
    stated = CostEngine(_model(), catalog, time_basis="monthly").total_cost()
    by_hand = CostEngine(_with_values(1.1, 8.0), catalog,
                         time_basis="monthly").total_cost()
    assert stated > 0
    assert stated == pytest.approx(by_hand)


def test_a_model_with_only_value_metrics_is_unchanged():
    engine = CostEngine(_with_values(1.1, 8.0), catalog=None, time_basis="monthly")
    assert engine.total_cost() == pytest.approx(HAND_COMPUTED_TOTAL)


# What-if over a parameter used in itemBytes moves the cost in steps.

def _query_model() -> dict:
    model = _model()
    model["workflow"]["parameters"] = {"orders_per_customer": 4}
    model["nodes"][TABLE]["usageMetrics"] = {
        "readRequests": {
            "unit": "ReadRequestUnits",
            "operations": [{"kind": "Query", "consistency": "strong",
                            "itemBytes": "orders_per_customer * 1024", "count": 1}],
        },
    }
    return model


def test_what_if_over_item_bytes_moves_the_cost_in_steps():
    """4 x 1024 is 4,096 bytes: one block. 5 x 1024 is 5,120: two blocks."""
    analyzer = SensitivityAnalyzer(_query_model(), None, time_basis="monthly")
    one_block = analyzer.what_if("orders_per_customer", 4)
    two_blocks = analyzer.what_if("orders_per_customer", 5)
    still_two = analyzer.what_if("orders_per_customer", 8)
    three_blocks = analyzer.what_if("orders_per_customer", 9)
    assert two_blocks == pytest.approx(2 * one_block)
    assert still_two == pytest.approx(two_blocks)
    assert three_blocks == pytest.approx(3 * one_block)


def test_a_parameter_that_makes_a_count_negative_names_the_operation():
    model = _model()
    model["workflow"]["parameters"]["writes_per_request"] = -1
    with pytest.raises(ValueError) as caught:
        CostEngine(model, None, time_basis="monthly").total_cost()
    message = str(caught.value)
    assert TABLE in message
    assert "writeRequests" in message
    assert "put-order" in message


# Validation: every error names the node and the metric.

def _errors(mutate) -> list[str]:
    model = _model()
    mutate(model)
    return validate_cost_model(model)


def _operations(model, metric="readRequests") -> list[dict]:
    return _metrics(model)[metric]["operations"]


def _names(errors, *fragments):
    assert errors, "expected a validation error"
    joined = "\n".join(errors)
    for fragment in (TABLE, *fragments):
        assert fragment in joined, f"{fragment!r} missing from:\n{joined}"


def test_operations_and_value_together_are_refused():
    errors = _errors(lambda m: _metrics(m)["readRequests"].update(value=3))
    _names(errors, "readRequests", "value", "operations")


def test_neither_value_nor_operations_is_refused():
    def mutate(model):
        del _metrics(model)["readRequests"]["operations"]
    errors = _errors(mutate)
    _names(errors, "readRequests", "value", "operations")


def test_neither_is_refused_by_compute_too():
    model = _model()
    del _metrics(model)["readRequests"]["operations"]
    with pytest.raises(ValueError) as caught:
        CostEngine(model, None, time_basis="monthly").total_cost()
    assert "readRequests" in str(caught.value)


def test_an_unknown_kind_is_refused():
    errors = _errors(lambda m: _operations(m)[0].update(kind="Fetch"))
    _names(errors, "readRequests", "get-order", "Fetch", "GetItem")


def test_an_unknown_consistency_is_refused():
    errors = _errors(lambda m: _operations(m)[1].update(consistency="quorum"))
    _names(errors, "readRequests", "list-orders", "quorum")


def test_a_negative_count_is_refused():
    errors = _errors(lambda m: _operations(m)[0].update(count=-1))
    _names(errors, "readRequests", "get-order", "count")


def test_a_negative_item_size_is_refused():
    errors = _errors(lambda m: _operations(m)[0].update(itemBytes=-5))
    _names(errors, "readRequests", "get-order", "itemBytes")


def test_an_empty_operations_list_is_refused():
    errors = _errors(lambda m: _metrics(m)["readRequests"].update(operations=[]))
    _names(errors, "readRequests", "operations")


def test_an_operation_without_a_name_is_named_by_position():
    def mutate(model):
        del _operations(model)[1]["name"]
        _operations(model)[1]["kind"] = "Fetch"
    errors = _errors(mutate)
    _names(errors, "readRequests", "position 2")


@pytest.mark.parametrize("kind,consistency", [
    ("TransactGetItems", "strong"),
    ("TransactGetItems", "eventual"),
])
def test_transact_get_items_is_always_transactional(kind, consistency):
    def mutate(model):
        _operations(model)[0].update(kind=kind, consistency=consistency)
    _names(_errors(mutate), "readRequests", "get-order", "transactional")


@pytest.mark.parametrize("consistency", ["eventual", "strong"])
def test_a_write_kind_refuses_a_read_consistency(consistency):
    def mutate(model):
        _operations(model, "writeRequests")[0].update(consistency=consistency)
    _names(_errors(mutate), "writeRequests", "put-order", consistency)


def test_mixing_reads_and_writes_in_one_metric_is_refused():
    def mutate(model):
        _operations(model).append(
            {"name": "put", "kind": "PutItem", "itemBytes": 10, "count": 1})
    _names(_errors(mutate), "readRequests", "read", "write")


def test_read_kinds_on_the_write_metric_are_refused():
    def mutate(model):
        _metrics(model)["writeRequests"]["operations"] = [
            {"name": "get", "kind": "GetItem", "itemBytes": 10, "count": 1}]
    _names(_errors(mutate), "writeRequests", "GetItem")


def test_an_operation_missing_a_field_is_refused_once():
    def mutate(model):
        del _operations(model)[0]["itemBytes"]
    errors = _errors(mutate)
    _names(errors, "readRequests", "get-order", "itemBytes")
    assert len([e for e in errors if "itemBytes" in e]) == 1


def test_operations_on_another_node_are_refused():
    model = _model()
    model["nodes"]["aws_sqs_queue.jobs"] = {
        "nodeType": "storage",
        "resourceAddress": "aws_sqs_queue.jobs",
        "provider": "aws",
        "service": "AmazonSQS",
        "region": "us-east-1",
        "usageMetrics": {"requests": {
            "unit": "requests",
            "operations": [{"kind": "GetItem", "itemBytes": 1, "count": 1}]}},
    }
    errors = validate_cost_model(model)
    assert any("aws_sqs_queue.jobs" in e and "requests" in e
               and "DynamoDB" in e for e in errors), errors


def test_the_command_line_validate_reports_the_errors(tmp_path, capsys):
    model = _model()
    _operations(model)[0]["kind"] = "Fetch"
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(model))
    assert main(["validate", str(path)]) == 1
    output = capsys.readouterr().out
    assert TABLE in output and "readRequests" in output and "Fetch" in output


def test_the_command_line_validate_accepts_the_example(tmp_path, capsys):
    path = tmp_path / "model.yaml"
    path.write_text(OPERATIONS)
    assert main(["validate", str(path)]) == 0


# The snapshot shows the units each operation contributes.

def test_compute_json_lists_the_units_of_each_operation(tmp_path, capsys):
    path = tmp_path / "model.yaml"
    path.write_text(OPERATIONS)
    assert main(["compute", str(path), "--format", "json",
                 "--time-basis", "monthly", "--pricing", "seed"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    metric = snapshot["nodes"][TABLE]["metrics"]["readRequests"]
    assert metric["quantity"] == pytest.approx(1_100_000)
    assert metric["operations"] == [
        {"name": "get-order", "kind": "GetItem", "consistency": "eventual",
         "itemBytes": 900.0, "count": 1.0, "unitsPerCall": 0.5, "units": 0.5},
        {"name": "list-orders", "kind": "Query", "consistency": "strong",
         "itemBytes": 9000.0, "count": 0.2, "unitsPerCall": 3.0, "units": 0.6},
    ]
    value_metric = _with_values(1.1, 8.0)
    path.write_text(yaml.safe_dump(value_metric))
    assert main(["compute", str(path), "--format", "json",
                 "--time-basis", "monthly", "--pricing", "seed"]) == 0
    plain = json.loads(capsys.readouterr().out)
    assert "operations" not in plain["nodes"][TABLE]["metrics"]["readRequests"]


def test_the_sdk_states_operations():
    from infra_cost_model.sdk.workflow import NodeUsage
    operations = [{"kind": "GetItem", "itemBytes": 900, "count": 1}]
    usage = NodeUsage().with_operations(
        "readRequests", operations, unit="ReadRequestUnits")
    assert usage.metrics["readRequests"] == {
        "unit": "ReadRequestUnits", "operations": operations}
