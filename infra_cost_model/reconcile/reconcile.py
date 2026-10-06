"""Compare a cost model with an actuals file (#444).

The engine reports what a model predicts. This module compares that prediction
with what the bill says, per group of nodes and bill lines.

Grouping is a connected-components problem, not a per-node loop (#444, trap 4).
Four container services under one ECS line would each be compared with the
whole line, counting it four times. So every node joins every line it maps to,
and each component is compared once.

The projection divides by the days the line was alive for, not by the days it
happened to bill (#444, traps 1 to 3):

    projected = window_sum / divisor_days * 30.4375

A billing-shape rule picks the divisor, and the report names the rule per line
(#462):

* `new` divides by the days since the line's first billed day. A line is new
  only if both hold: no earlier day in the whole actuals file billed it, and it
  bills on every day from its first billed day to the end of the file, for at
  least `new_line_days` days (default 7). Every day the file carries counts as
  settled.
* `zero-fill` divides by the whole window. Every other line takes it, so a
  charge that bills once a quarter or once a year projects at its share of the
  window, not at a run-rate.

* A line that bills once a month, reporting `$0.00` for the other days, divides
  by the window rather than by the one day it charged. That is the difference
  between a charge at its own size and a charge at 30.4 times its size.
* A day Cost Explorer omits is still a day in the window, so a sparse line
  divides by the days it could have billed, not by the few it reported.
* A resource that started inside the window and bills daily divides by the days
  since it started. A rare charge that lands inside the window does not,
  because it has gaps after its first billed day.
"""

from dataclasses import dataclass, field
from typing import Optional

from infra_cost_model.reconcile.actuals import (
    DAYS_PER_MONTH,
    Actuals,
    BillLineKey,
    ReconcileError,
)
from infra_cost_model.reconcile.billing import BillLine, node_billing_lines
from infra_cost_model.reconcile.config import ReconcileConfig

DEFAULT_WINDOW_DAYS = 30
DEFAULT_NEW_LINE_DAYS = 7

RULE_NEW, RULE_ZERO_FILL = "new", "zero-fill"

_OK, _WARN, _FAIL, _UNREADABLE = "ok", "warn", "fail", "unreadable"
_RANK = {_OK: 0, _WARN: 1, _FAIL: 2, _UNREADABLE: 3}


@dataclass
class BillLineResult:
    """One bill line, and how its window was read."""

    key: BillLineKey
    projected: float
    divisor_days: int
    rule: str
    billed_days: int
    zero_days: int
    absent_days: int
    window_days: int
    first_billed_day: Optional[str] = None
    last_billed_day: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "service": self.key.service,
            "usageType": self.key.usage_type,
            "label": self.key.label(),
            "projected": self.projected,
            "divisorDays": self.divisor_days,
            "divisorRule": self.rule,
            "billedDays": self.billed_days,
            "zeroDays": self.zero_days,
            "absentDays": self.absent_days,
            "windowDays": self.window_days,
            "firstBilledDay": self.first_billed_day,
            "lastBilledDay": self.last_billed_day,
        }


@dataclass
class Group:
    """A connected component of nodes and bill lines, compared once."""

    nodes: list[str]
    lines: list[BillLineKey]
    modelled: float
    projected: float
    bill_lines: list[BillLineResult]
    drift_usd: float
    drift_pct: Optional[float]
    status: str

    def to_dict(self) -> dict:
        return {
            "nodes": self.nodes,
            "lines": [{"service": key.service, "usageType": key.usage_type}
                      for key in self.lines],
            "modelled": self.modelled,
            "projected": self.projected,
            "driftUsd": self.drift_usd,
            "driftPct": self.drift_pct,
            "status": self.status,
            "billLines": [line.to_dict() for line in self.bill_lines],
        }


@dataclass
class UnmodelledResult:
    """A bill line the model leaves out, with the reason it was accepted."""

    service: str
    usage_type: Optional[str]
    reason: str
    projected: float

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "usageType": self.usage_type,
            "reason": self.reason,
            "projected": self.projected,
        }


@dataclass
class Reconciliation:
    """Everything the command reports, for the text table and for JSON."""

    window_days: int
    window_start: Optional[str]
    window_end: Optional[str]
    warn_pct: float
    fail_pct: float
    days_reported: int
    truncated: bool
    error: Optional[str]
    groups: list[Group]
    unmodelled: list[UnmodelledResult]
    unplaced: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        """The worst status any group carries."""
        if not self.groups:
            return _UNREADABLE if self.error or self.truncated else _OK
        return max((group.status for group in self.groups), key=_RANK.__getitem__)

    @property
    def totals(self) -> dict:
        """Modelled and projected across every compared group."""
        modelled = sum(group.modelled for group in self.groups)
        projected = sum(group.projected for group in self.groups)
        drift = modelled - projected
        return {
            "modelled": modelled,
            "projected": projected,
            "driftUsd": drift,
            "driftPct": (drift / projected * 100) if projected else None,
            "status": self.status,
        }

    def to_dict(self) -> dict:
        return {
            "windowDays": self.window_days,
            "windowStart": self.window_start,
            "windowEnd": self.window_end,
            "warnPct": self.warn_pct,
            "failPct": self.fail_pct,
            "daysReported": self.days_reported,
            "truncated": self.truncated,
            "error": self.error,
            "status": self.status,
            "totals": self.totals,
            "groups": [group.to_dict() for group in self.groups],
            "unmodelled": [entry.to_dict() for entry in self.unmodelled],
            "unplaced": list(self.unplaced),
        }


class _Components:
    """Union-find over node addresses and bill lines."""

    def __init__(self):
        self._parent: dict[tuple, tuple] = {}

    def find(self, item: tuple) -> tuple:
        self._parent.setdefault(item, item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, left: tuple, right: tuple) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            # Ordered by a string key: a BillLineKey carries no ordering of its
            # own, and comparing two of them raises.
            low, high = sorted((a, b), key=_member_sort_key)
            self._parent[high] = low

    def groups(self) -> dict[tuple, list[tuple]]:
        members: dict[tuple, list[tuple]] = {}
        for item in self._parent:
            members.setdefault(self.find(item), []).append(item)
        return members


def _member_sort_key(member: tuple) -> str:
    """A stable ordering over a union-find member, node or bill line."""
    kind, value = member
    return f"{kind}\u0000{value.label() if isinstance(value, BillLineKey) else value}"


def _covered_keys(line: BillLine, actuals: Actuals) -> list[BillLineKey]:
    """The bill lines a node's declared line covers.

    A declared line that names a usage type covers exactly that line. One that
    names only a service covers every usage type of that service, so a model
    that does not distinguish usage types still compares against the whole
    bill. When the file has no such line the declared key stands alone, which
    is what projects it at zero instead of dropping it from the report.
    """
    if line.usage_type is not None:
        return [line.key]
    return [BillLineKey(service, usage_type)
            for service, usage_type in sorted(actuals.lines)
            if service == line.service] or [line.key]


def _window(actuals: Actuals, window_days: int) -> list[str]:
    """The trailing `window_days` days the file reports, or all of them."""
    if window_days <= 0:
        raise ReconcileError("--window-days must be a positive number of days")
    return actuals.days[-window_days:]


def _project(actuals_daily: dict[str, float], all_days: list[str],
             window: list[str],
             new_line_days: int = DEFAULT_NEW_LINE_DAYS) -> BillLineResult:
    """Project a bill line's window spend out to a month.

    The billing-shape rule picks the divisor (#462). A line is new when no
    earlier day in the file billed it and it bills on every day from its first
    billed day to the end of the file, for at least `new_line_days` days. A new
    line divides by the days since its first billed day. Any other line
    zero-fills: it divides by the whole window.
    """
    billed = [day for day in window if actuals_daily.get(day, 0.0) != 0.0]
    ever_billed = [day for day in all_days if actuals_daily.get(day, 0.0) != 0.0]
    zero_days = sum(1 for day in window
                    if day in actuals_daily and actuals_daily[day] == 0.0)
    rule = RULE_ZERO_FILL
    divisor_days = 0
    if ever_billed and window:
        first = ever_billed[0]
        since_first = [day for day in all_days if day >= first]
        steady = all(actuals_daily.get(day, 0.0) != 0.0 for day in since_first)
        alive_from = window[0]
        if steady and len(since_first) >= new_line_days:
            rule = RULE_NEW
            alive_from = max(first, window[0])
        divisor_days = sum(1 for day in window if day >= alive_from)
    total = sum(actuals_daily.get(day, 0.0) for day in window)
    projected = (total / divisor_days * DAYS_PER_MONTH) if divisor_days else 0.0
    return BillLineResult(
        key=None,
        projected=projected,
        divisor_days=divisor_days,
        rule=rule,
        billed_days=len(billed),
        zero_days=zero_days,
        absent_days=len(window) - len(billed) - zero_days,
        window_days=len(window),
        first_billed_day=billed[0] if billed else None,
        last_billed_day=billed[-1] if billed else None,
    )


def _status(drift_usd: float, projected: float, config: ReconcileConfig) -> tuple:
    """The drift in dollars and the status it earns, as a percent."""
    if projected:
        drift_pct = drift_usd / projected * 100
        if abs(drift_pct) > config.fail_pct:
            return drift_pct, _FAIL
        if abs(drift_pct) > config.warn_pct:
            return drift_pct, _WARN
        return drift_pct, _OK
    # The model predicts spend on a line the window bills nothing. The drift is
    # undefined rather than infinite, and it fails: some line is unaccounted for.
    return None, (_FAIL if drift_usd else _OK)


def reconcile(model: dict, costs: dict, actuals: Actuals,
              config: Optional[ReconcileConfig] = None,
              window_days: int = DEFAULT_WINDOW_DAYS,
              new_line_days: int = DEFAULT_NEW_LINE_DAYS) -> Reconciliation:
    """Compare a model's monthly cost with what the bill says.

    Args:
        model: the cost model representation, whose nodes declare their
            `billingLines` (#442).
        costs: node address to modelled monthly cost, as the engine reports it
            on a monthly time basis.
        actuals: per-day spend read from an actuals file.
        config: thresholds and accepted gaps; defaults apply when None.
        window_days: the trailing days of the actuals file to compare.
        new_line_days: the fewest consecutive billed days, ending at the end of
            the file, that make a line new rather than zero-filled (#462).

    Returns:
        A Reconciliation, one group per connected component.
    """
    config = config or ReconcileConfig()
    window = _window(actuals, window_days)
    if new_line_days <= 0:
        raise ReconcileError("--new-line-days must be a positive number of days")

    dropped, unmodelled = _split_allowlisted(model, actuals, config, window,
                                             new_line_days)
    remaining = Actuals(
        lines={line: amounts for line, amounts in actuals.lines.items()
               if line not in dropped},
        days=actuals.days,
        truncated=actuals.truncated,
        error=actuals.error,
    )

    components = _Components()
    node_lines: dict[str, list] = {}
    unplaced: list[str] = []
    for address, node in (model.get("nodes") or {}).items():
        lines = node_billing_lines(address, node)
        node_lines[address] = lines
        if not lines:
            if costs.get(address):
                unplaced.append(address)
            continue
        for line in lines:
            for key in _covered_keys(line, remaining):
                components.union(("node", address), ("line", key))

    # Every line the file reported, whether or not a node maps to it. A line no
    # node claims is spend the model doesn't predict, and it compares as such.
    for service, usage_type in remaining.lines:
        components.union(("line", BillLineKey(service, usage_type)),
                         ("line", BillLineKey(service, usage_type)))

    groups = []
    for members in components.groups().values():
        nodes = sorted(item[1] for item in members if item[0] == "node")
        lines = sorted((item[1] for item in members if item[0] == "line"),
                       key=lambda key: key.label())
        if not lines:
            # A component always holds the line its node was grouped by, so this
            # is defensive. It keeps the label below from indexing an empty list
            # if that invariant ever breaks.
            continue
        modelled = sum(costs.get(address, 0.0) for address in nodes)
        results = [_project(remaining.daily(key), remaining.days, window,
                           new_line_days)
                   for key in lines]
        for key, result in zip(lines, results):
            result.key = key
        projected = sum(result.projected for result in results)
        drift_usd = modelled - projected
        drift_pct, status = _status(drift_usd, projected, config)
        groups.append(Group(
            nodes=nodes, lines=lines, modelled=modelled, projected=projected,
            bill_lines=results, drift_usd=drift_usd, drift_pct=drift_pct,
            status=_UNREADABLE if not actuals.readable else status,
        ))

    # Worst first: the rank is negated rather than reversed because the drift
    # and label that follow it are already in ascending order.
    groups.sort(key=lambda group: (
        _RANK[group.status] * -1, -abs(group.drift_usd),
        group.lines[0].label() if group.lines else "",
    ))

    return Reconciliation(
        window_days=len(window),
        window_start=window[0] if window else None,
        window_end=window[-1] if window else None,
        warn_pct=config.warn_pct,
        fail_pct=config.fail_pct,
        days_reported=len(actuals.days),
        truncated=actuals.truncated,
        error=actuals.error,
        groups=groups,
        unmodelled=unmodelled,
        unplaced=sorted(unplaced),
    )


def _split_allowlisted(model: dict, actuals: Actuals, config: ReconcileConfig,
                       window: list[str], new_line_days: int = DEFAULT_NEW_LINE_DAYS):
    """Take the allowlisted lines out of the comparison, with their reasons.

    An allowlist entry that a node also maps to is refused rather than applied:
    dropping the line would delete the comparison instead of accepting a gap,
    and the model would quietly stop being checked.
    """
    mapped = set()
    for address, node in (model.get("nodes") or {}).items():
        for line in node_billing_lines(address, node):
            mapped.update(_covered_keys(line, actuals))

    dropped: set[tuple] = set()
    unmodelled: list[UnmodelledResult] = []
    for (service, usage_type), _amounts in sorted(actuals.lines.items()):
        entry = config.entry_for(service, usage_type)
        if entry is None:
            continue
        key = BillLineKey(service, usage_type)
        if key in mapped:
            raise ReconcileError(
                f"reconcile.yaml allowlists {service} as unmodelled, but a node maps "
                f"to that line; allowlisting it would drop the comparison"
            )
        dropped.add((service, usage_type))
        unmodelled.append(UnmodelledResult(
            service=service, usage_type=usage_type, reason=entry.reason,
            projected=_project(actuals.lines[(service, usage_type)],
                               actuals.days, window, new_line_days).projected,
        ))
    return dropped, unmodelled