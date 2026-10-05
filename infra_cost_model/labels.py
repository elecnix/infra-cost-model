"""Node labels: free-form key/value pairs that slice a model's costs (#445).

A node states ``labels: {category: llm, env: prod}``. Labels never change
derived usage and never change a price (DP#6), so they belong to the
presentation layer rather than the engine: ``compute`` and ``analyze`` group
costs by a label or leave out the nodes that carry one.
"""

from typing import Optional

# What the nodes without a value for the grouped key report under. A label key
# is optional on a node, so the bucket that holds those nodes needs a name.
UNLABELED = "(unlabeled)"


def node_labels(node: dict) -> dict:
    """The labels of one node, or an empty mapping when it carries none."""
    labels = node.get("labels") if isinstance(node, dict) else None
    return labels if isinstance(labels, dict) else {}


def _non_string_labels(node: dict) -> dict:
    """The labels of one node whose value is not a string, keyed by label."""
    return {key: value for key, value in node_labels(node).items()
            if not isinstance(value, str)}


def label_value_problem(nodes: dict) -> Optional[str]:
    """Why a node's label value cannot be used, or None when every one can.

    The schema types a label value as a string, but a caller reads labels from
    the parsed YAML without running `validate`, so the rule holds here too. A
    non-string value would otherwise group and print as itself, or crash the
    sort that orders the groups.
    """
    for address in sorted(nodes or {}):
        for key, value in sorted(_non_string_labels(nodes[address]).items()):
            return f"node {address!r} has a non-string value for label {key!r}: {value!r}"
    return None


def label_keys(nodes: dict) -> set:
    """Every label key any node uses."""
    keys = set()
    for node in (nodes or {}).values():
        keys.update(node_labels(node))
    return keys


def parse_label_selector(text: str) -> tuple:
    """Split a ``key=value`` selector into its two halves.

    Raises ValueError on anything else, so a mistyped selector fails with a
    message rather than matching nothing silently.
    """
    key, sep, value = text.partition("=")
    if not sep or not key.strip() or not value:
        raise ValueError(f"a label selector is key=value, got '{text}'")
    return key.strip(), value.strip()


def excluded_addresses(nodes: dict, selectors: list) -> set:
    """The node addresses carrying any one of the ``(key, value)`` selectors."""
    excluded = set()
    for address, node in (nodes or {}).items():
        labels = node_labels(node)
        if any(labels.get(key) == value for key, value in selectors):
            excluded.add(address)
    return excluded


def group_nodes(nodes: dict, costs: dict, key: str) -> list:
    """Group ``costs`` by the value of label ``key``.

    Returns ``(label, subtotal, addresses)`` triples sorted by label value, with
    the nodes that carry no value for ``key`` last under UNLABELED. A node
    counts once, under its own value, whatever else the key says about it. An
    address in ``costs`` that the model does not declare has no labels and so
    lands in the UNLABELED bucket.
    """
    grouped: dict = {}
    for address, cost in (costs or {}).items():
        value = node_labels((nodes or {}).get(address, {})).get(key, UNLABELED)
        entry = grouped.setdefault(value, [0.0, []])
        entry[0] += cost
        entry[1].append(address)

    ordered = sorted(v for v in grouped if v != UNLABELED)
    if UNLABELED in grouped:
        ordered.append(UNLABELED)
    return [(value, grouped[value][0], sorted(grouped[value][1])) for value in ordered]