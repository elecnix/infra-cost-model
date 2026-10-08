"""A diffable cost snapshot of a computed model (#443).

The table the CLI prints is meant for a person, so it shows only totals.
A reviewer comparing two revisions of a model wants to see which node moved
and which usage metric moved it, and a machine comparing them wants the
numbers to be stable. ``build_snapshot`` therefore reports every metric's
quantity, its effective unit price, its cost and the source of its price,
with the money rounded and the keys sorted, so two snapshots of one model
differ only where the model did.
"""

import json

from infra_cost_model import __version__

# The version of the snapshot's own shape, raised when its keys change.
SNAPSHOT_SCHEMA_VERSION = 1

# Money and quantities keep six decimal places, matching the CLI table, or
# six significant digits, whichever keeps more. A per-second cost or a unit
# price below a dollar then keeps its digits: 6.25e-7 prints as itself, not
# as 0.000001, and a node's metrics add up to its total within half a unit
# in the last digit each number keeps.
_DECIMALS = 6
_SIGNIFICANT_DIGITS = 6


def _round(value: float) -> float:
    """Round a number for a snapshot, keeping six decimals or six digits."""
    if value != 0.0 and abs(value) < 1.0:
        return float(f"{value:.{_SIGNIFICANT_DIGITS}g}")
    return round(value, _DECIMALS) + 0.0


def _metric_entry(record) -> dict:
    """One priced metric, rounded like the rest of the snapshot."""
    return {
        "quantity": _round(record.quantity),
        "unitPrice": _round(_unit_price(record)),
        "cost": _round(record.cost),
        "fixed": record.fixed,
        "priceSource": record.price_source,
    }


def _unit_price(record) -> float:
    """The effective rate of a metric: what one unit of it costs."""
    if not record.quantity:
        return 0.0
    return record.cost / record.quantity


def _unpriced(metric) -> dict:
    """One unpriced metric, with its quantity rounded like a priced one."""
    entry = metric.to_dict()
    entry["quantity"] = _round(entry["quantity"])
    return entry


def build_snapshot(engine) -> dict:
    """Summarize a completed ``CostEngine`` run as a JSON-ready dict.

    The engine must have run ``compute`` first. Every number is one the
    engine itself computed, rounded rather than recomputed, so a snapshot
    agrees with the table down to the printed decimal and a diff shows the
    node and the usage metric that changed. A node's metrics therefore add up
    to its total to the last printed decimal rather than exactly, while the
    node total and the model total are the engine's own totals rounded, as
    the table prints them.
    """
    nodes: dict[str, dict] = {}
    for address in sorted(engine.costs):
        metrics = {
            name: _metric_entry(record)
            for name, record in sorted(
                engine.metric_costs.get(address, {}).items())
        }
        total = _round(engine.costs[address])
        fixed = _round(sum(record.cost for record in
                           engine.metric_costs.get(address, {}).values()
                           if record.fixed))
        nodes[address] = {
            "total": total,
            "fixed": fixed,
            "variable": _round(total - fixed),
            "metrics": metrics,
        }

    return {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "engineVersion": __version__,
        "timeBasis": engine.time_basis,
        "total": _round(sum(engine.costs.values())),
        "nodes": nodes,
        "unpriced": [
            _unpriced(metric)
            for metric in sorted(engine.unpriced_metrics,
                                 key=lambda m: (m.node, m.metric))
        ],
    }


def render_snapshot(snapshot: dict) -> str:
    """Render a snapshot as the JSON document the CLI prints."""
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"
