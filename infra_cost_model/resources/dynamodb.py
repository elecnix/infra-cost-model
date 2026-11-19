"""DynamoDB resource model implementation."""

import math
from typing import Callable

from .types import (
    OperationUnits,
    OperationsQuantity,
    ResourceExtract,
    StorageResource,
)

# The on-demand request unit rule, from the Amazon DynamoDB Developer Guide
# pages "DynamoDB on-demand capacity mode" (section "Read request units and
# write request units") and "DynamoDB read and write operations":
#
# - One read request unit is one strongly consistent read of an item up to
#   4 KB. An eventually consistent read costs half of that, and a
#   transactional read costs twice.
# - One write request unit is one write of an item up to 1 KB. A
#   transactional write costs twice.
# - A larger item bills one more unit for each further block, rounded up.
#
# Every number of the rule is stated here and nowhere else.
READ_BLOCK_BYTES = 4096
WRITE_BLOCK_BYTES = 1024
EVENTUAL_READ_FACTOR = 0.5
STRONG_READ_FACTOR = 1.0
TRANSACTIONAL_FACTOR = 2.0
STANDARD_WRITE_FACTOR = 1.0

# Each operation kind a metric can state, and the family of request unit it
# bills. A read that finds no item still bills the minimum, so a size floors
# at one byte.
OPERATION_KINDS = {
    "GetItem": "read",
    "BatchGetItem": "read",
    "Query": "read",
    "Scan": "read",
    "TransactGetItems": "read",
    "PutItem": "write",
    "UpdateItem": "write",
    "DeleteItem": "write",
    "BatchWriteItem": "write",
    "TransactWriteItems": "write",
}

READ_FACTORS = {
    "eventual": EVENTUAL_READ_FACTOR,
    "strong": STRONG_READ_FACTOR,
    "transactional": TRANSACTIONAL_FACTOR,
}
DEFAULT_READ_CONSISTENCY = "eventual"
TRANSACTIONAL = "transactional"
CONSISTENCIES = tuple(READ_FACTORS)

# The usage metric names that hold each family. The catalog row names stay
# accepted, the way the rest of the handler accepts them (#427).
_METRICS_BY_FAMILY = {
    "read": ("readRequests", "Dynamo-ReadRequest"),
    "write": ("writeRequests", "Dynamo-WriteRequest"),
}


def request_units(kind: str, consistency: str | None, item_bytes: float) -> float:
    """Request units one call of ``kind`` bills for ``item_bytes`` of data.

    ``consistency`` is ``eventual`` (the default for a read), ``strong`` or
    ``transactional``. ``TransactGetItems`` and ``TransactWriteItems`` are
    always transactional. The caller has checked that the kind and the
    consistency go together, which ``DynamoDBTable.operations_errors`` does.
    """
    size = max(item_bytes, 1)
    if OPERATION_KINDS[kind] == "read":
        if kind == "TransactGetItems":
            consistency = TRANSACTIONAL
        factor = READ_FACTORS[consistency or DEFAULT_READ_CONSISTENCY]
        return math.ceil(size / READ_BLOCK_BYTES) * factor
    transactional = kind == "TransactWriteItems" or consistency == TRANSACTIONAL
    factor = TRANSACTIONAL_FACTOR if transactional else STANDARD_WRITE_FACTOR
    return math.ceil(size / WRITE_BLOCK_BYTES) * factor


def _label(operation: dict, position: int) -> str:
    name = operation.get("name")
    if isinstance(name, str) and name:
        return f"operation '{name}'"
    return f"operation at position {position}"


def _stated_consistency(operation: dict) -> str:
    """The consistency an operation bills at, once its kind has a family."""
    kind = operation["kind"]
    if kind in ("TransactGetItems", "TransactWriteItems"):
        return TRANSACTIONAL
    consistency = operation.get("consistency")
    if OPERATION_KINDS[kind] == "read":
        return consistency or DEFAULT_READ_CONSISTENCY
    return consistency or "standard"


class DynamoDBTable(StorageResource):
    """DynamoDB table - storage node (leaf, no outgoing edges)."""

    @property
    def valid_metrics(self) -> list[str]:
        return ["readRequests", "writeRequests", "storageGb",
                "readCapacityUnitHours", "writeCapacityUnitHours"]

    @property
    def catalog_metrics(self) -> dict[str, str]:
        # An on-demand table bills requests. A provisioned table bills each
        # hour of each capacity unit it provisions, the units of its global
        # secondary indexes included, so a model states readCapacity x 730
        # hours a month instead of a request count (#441).
        return {"readRequests": "Dynamo-ReadRequest",
                "writeRequests": "Dynamo-WriteRequest",
                "storageGb": "Dynamo-Storage",
                "readCapacityUnitHours": "Dynamo-RCU-Hour",
                "writeCapacityUnitHours": "Dynamo-WCU-Hour"}

    @property
    def supports_operations(self) -> bool:
        return True

    def operations_errors(self, metric_name: str, operations: list) -> list[str]:
        if not isinstance(operations, list):
            return []  # The schema reports a value that is not a list.
        if not operations:
            return ["'operations' is empty. State at least one operation, "
                    "or state 'value' instead."]

        errors: list[str] = []
        families: dict[str, list[str]] = {}
        for position, operation in enumerate(operations, start=1):
            if not isinstance(operation, dict):
                continue  # The schema reports an operation that is no mapping.
            label = _label(operation, position)
            kind = operation.get("kind")
            consistency = operation.get("consistency")

            if kind is None:
                errors.append(f"{label}: no 'kind'. Kinds: "
                              f"{', '.join(OPERATION_KINDS)}.")
            elif kind not in OPERATION_KINDS:
                errors.append(f"{label}: unknown kind '{kind}'. Kinds: "
                              f"{', '.join(OPERATION_KINDS)}.")
            else:
                families.setdefault(OPERATION_KINDS[kind], []).append(kind)

            if consistency is not None and consistency not in CONSISTENCIES:
                errors.append(f"{label}: unknown consistency '{consistency}'. "
                              f"Consistencies: {', '.join(CONSISTENCIES)}.")
            elif kind in OPERATION_KINDS and consistency is not None:
                if kind == "TransactGetItems" and consistency != TRANSACTIONAL:
                    errors.append(
                        f"{label}: TransactGetItems is always transactional, "
                        f"so it cannot state consistency '{consistency}'.")
                elif (OPERATION_KINDS[kind] == "write"
                        and consistency != TRANSACTIONAL):
                    errors.append(
                        f"{label}: {kind} is a write, so it cannot state "
                        f"consistency '{consistency}'. A write is standard or "
                        f"'transactional'.")

            for field in ("itemBytes", "count"):
                value = operation.get(field)
                if value is None:
                    errors.append(f"{label}: no '{field}'.")
                elif (isinstance(value, (int, float))
                        and not isinstance(value, bool) and value < 0):
                    errors.append(f"{label}: '{field}' is {value}, which is "
                                  f"less than 0.")

        if len(families) > 1:
            errors.append(
                f"'operations' mixes read kinds ({', '.join(families['read'])}) "
                f"and write kinds ({', '.join(families['write'])}). One metric "
                f"holds one family: move one into its own metric.")
        elif families:
            (family,) = families
            if metric_name not in _METRICS_BY_FAMILY[family]:
                holder = _METRICS_BY_FAMILY[family][0]
                errors.append(
                    f"'operations' states {family} kinds "
                    f"({', '.join(families[family])}), which bill under "
                    f"'{holder}', not under '{metric_name}'.")
        return errors

    def operations_quantity(
            self, metric_name: str, operations: list,
            evaluate: Callable[[object, str], float]) -> OperationsQuantity:
        lines = []
        for position, operation in enumerate(operations, start=1):
            label = _label(operation, position)
            try:
                count = evaluate(operation["count"], "count")
                item_bytes = evaluate(operation["itemBytes"], "itemBytes")
            except ValueError as error:
                raise ValueError(f"{label}: {error}") from None
            kind = operation["kind"]
            lines.append(OperationUnits(
                name=operation.get("name"),
                kind=kind,
                consistency=_stated_consistency(operation),
                item_bytes=item_bytes,
                count=count,
                units_per_call=request_units(
                    kind, operation.get("consistency"), item_bytes),
            ))
        return OperationsQuantity(operations=tuple(lines))

    @classmethod
    def from_address(cls, resource_address: str) -> StorageResource | None:
        """Parse resource address to determine if it's a DynamoDB table."""
        if resource_address.startswith("aws_dynamodb_table.") or \
           resource_address.startswith("aws.dynamodb.Table:") or \
           resource_address.startswith("aws:dynamodb:Table:") or \
           "DynamoDB::Table:" in resource_address:
            return cls()
        return None

    @classmethod
    def extract_tf(cls, resource: dict) -> ResourceExtract:
        """Extract from Terraform aws_dynamodb_table resource."""
        values = resource.get("values", {})

        return ResourceExtract(
            resource_address=resource.get("address", ""),
            node_type="storage",
            provider="aws",
            service="AmazonDynamoDB",
            region=values.get("region"),
            config={
                "billingMode": values.get("billing_mode"),
                "hashKey": values.get("hash_key"),
                "rangeKey": values.get("range_key"),
                "readCapacity": values.get("read_capacity"),
                "writeCapacity": values.get("write_capacity"),
            }
        )

    @classmethod
    def extract_pulumi(cls, resource: dict) -> ResourceExtract:
        """Extract from Pulumi aws.dynamodb.Table resource."""
        inputs = resource.get("inputs", {})

        return ResourceExtract(
            resource_address=resource.get("id", ""),
            node_type="storage",
            provider="aws",
            service="AmazonDynamoDB",
            region=inputs.get("region"),
            config={
                "billingMode": inputs.get("billingMode"),
                "hashKey": inputs.get("hashKey"),
                "rangeKey": inputs.get("rangeKey"),
                "readCapacity": inputs.get("readCapacity"),
                "writeCapacity": inputs.get("writeCapacity"),
            }
        )

    @classmethod
    def extract_cdk(cls, resource: dict) -> ResourceExtract:
        """Extract from CDK CloudFormation DynamoDB::Table."""
        properties = resource.get("Properties", {})
        key_schema = properties.get("KeySchema", [])
        billing_mode = properties.get("BillingMode", "PAY_PER_REQUEST")

        return ResourceExtract(
            resource_address=resource.get("LogicalId", ""),
            node_type="storage",
            provider="aws",
            service="AmazonDynamoDB",
            region=None,
            config={
                "billingMode": billing_mode,
                "hashKey": key_schema[0].get("AttributeName") if key_schema else None,
                "rangeKey": key_schema[1].get("AttributeName") if len(key_schema) > 1 else None,
                "readCapacity": properties.get("ProvisionedThroughput", {}).get("ReadCapacityUnits"),
                "writeCapacity": properties.get("ProvisionedThroughput", {}).get("WriteCapacityUnits"),
            }
        )
