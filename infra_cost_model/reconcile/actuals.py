"""Read an actuals file exported from AWS Cost Explorer (#444).

The payload is what `aws ce get-cost-and-usage` prints: `ResultsByTime` holds
one entry per day, each with a `TimePeriod` and groups keyed by `SERVICE`,
`SERVICE/USAGE_TYPE`, or two keys grouped by `SERVICE` then `USAGE_TYPE`, in
that order.
Nothing here calls AWS. A payload that failed, came back truncated, or reports
no results at all is recorded as unreadable rather than as zero spend, because
zero spend and missing data must not look the same (#444, trap 5).
"""

import json
import math
from datetime import date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# The days in an average month, 365.25 / 12, the same figure the pricing
# layer uses to turn a daily quota into a monthly allowance.
DAYS_PER_MONTH = 365.25 / 12


class ReconcileError(ValueError):
    """A reconciliation cannot run: a bad actuals file or a bad config."""


@dataclass(frozen=True)
class BillLineKey:
    """One line of the bill, as the exporter names it.

    ``usage_type`` is None when the model declares only a service. That line
    then covers every usage type the exporter reported under the service.
    """

    service: str
    usage_type: Optional[str] = None

    def label(self) -> str:
        """The exporter's own key, for a report a reader can grep the API for."""
        return self.service if self.usage_type is None else f"{self.service}/{self.usage_type}"


@dataclass
class Actuals:
    """Per-day spend read from an actuals file.

    ``lines`` is keyed by the exact (service, usage type) pair the exporter
    reported. ``truncated`` and ``error`` describe the file as a whole: a
    truncated export hides whichever lines the next page carried, so no line
    can be called complete.
    """

    lines: dict[tuple[str, Optional[str]], dict[str, float]] = field(default_factory=dict)
    days: list[str] = field(default_factory=list)
    truncated: bool = False
    error: Optional[str] = None

    @property
    def readable(self) -> bool:
        """True when the file holds a complete set of days."""
        return not self.truncated and self.error is None

    def daily(self, key: BillLineKey) -> dict[str, float]:
        """The per-day amounts of the lines `key` covers.

        A key with no usage type covers every usage type of the service, so a
        model that does not distinguish usage types still compares against the
        whole bill rather than one slice of it.
        """
        if key.usage_type is not None:
            return self.lines.get((key.service, key.usage_type), {})
        merged: dict[str, float] = {}
        for (service, _usage_type), amounts in self.lines.items():
            if service != key.service:
                continue
            for day, amount in amounts.items():
                merged[day] = merged.get(day, 0.0) + amount
        return merged

    def services(self) -> set[str]:
        return {service for service, _usage_type in self.lines}


def _amount(group: dict) -> tuple[Optional[float], object]:
    """The unblended cost of a group, falling back to the blended cost.

    Returns the amount and the raw value it was read from. The value is None
    when the group carries no readable amount at all: a cost the reader cannot
    parse is missing data, and turning it into 0.0 would report a fully costed
    model as drifting against a bill that says nothing (#444, trap 5).
    """
    metrics = group.get("Metrics") or {}
    for name in ("UnblendedCost", "BlendedCost"):
        metric = metrics.get(name)
        if isinstance(metric, dict) and "Amount" in metric:
            raw = metric["Amount"]
            try:
                amount = float(raw)
            except (TypeError, ValueError):
                return None, raw
            # "NaN" and "Infinity" parse, but compare false against every
            # threshold, so they would read as a match (#444, trap 5).
            return (amount, raw) if math.isfinite(amount) else (None, raw)
    return None, None


def _line_of(keys: list) -> Optional[tuple[str, Optional[str]]]:
    """The bill line a group's `Keys` name, or None when they name nothing.

    Two keys read as `(service, usage_type)`, service first. The payload does
    not record the `GroupBy` order, so a query must group by `SERVICE` and then
    `USAGE_TYPE`:

        --group-by Type=DIMENSION,Key=SERVICE Type=DIMENSION,Key=USAGE_TYPE

    An empty usage type means the exporter named none, so the line covers the
    service as a whole. The joined form `SERVICE/` reads the same way.

    One key is a service, or the joined `SERVICE/USAGE_TYPE` form.
    """
    if not keys or not isinstance(keys[0], str):
        return None
    if len(keys) >= 2 and isinstance(keys[1], str):
        return keys[0], keys[1] or None
    return _split_key(keys[0])


def _split_key(key: str) -> tuple[str, Optional[str]]:
    """`SERVICE/USAGE_TYPE` splits into its two parts.

    A key with no slash is a service queried without a usage type, so the line
    covers the service as a whole.
    """
    service, _, usage_type = key.partition("/")
    return service, usage_type or None


def _error_from(payload: dict) -> Optional[str]:
    """The message from a Cost Explorer or AWS API error payload."""
    for field_name in ("__type", "code", "Code", "Error"):
        value = payload.get(field_name)
        if isinstance(value, str) and value:
            message = payload.get("message") or payload.get("Message") or ""
            return f"{value}: {message}".strip(": ").strip()
    return None


def _is_iso_date(text: str) -> bool:
    """True when `text` is a calendar date written `YYYY-MM-DD`."""
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return len(text) == 10


def _period_start(period: dict) -> Optional[str]:
    """The day a `ResultsByTime` row covers.

    The CLI writes it under `TimePeriod`. `Time` stays accepted for files built
    by hand. The caller checks that the value is an ISO date.
    """
    for name in ("TimePeriod", "Time"):
        span = period.get(name)
        if isinstance(span, dict) and isinstance(span.get("Start"), str):
            return span["Start"]
    return None


def parse_actuals(payload: dict) -> Actuals:
    """Turn a decoded `get-cost-and-usage` payload into per-line per-day spend."""
    if not isinstance(payload, dict):
        raise ReconcileError(
            "actuals must be a JSON object from `aws ce get-cost-and-usage`"
        )

    error = _error_from(payload)
    results = payload.get("ResultsByTime")
    if error is None and not isinstance(results, list):
        error = "the payload carries no ResultsByTime array"

    actuals = Actuals(error=error, truncated=bool(payload.get("NextPageToken")))
    if not isinstance(results, list):
        return actuals

    unreadable: Optional[str] = None
    days: list[str] = []
    for period in results:
        if not isinstance(period, dict):
            continue
        start = _period_start(period)
        if start is None:
            continue
        if not _is_iso_date(start):
            unreadable = unreadable or (
                f"a ResultsByTime row starts on {start!r}, which is not an ISO date (YYYY-MM-DD)"
            )
            continue
        days.append(start)
        for group in period.get("Groups") or []:
            keys = group.get("Keys") or []
            line = _line_of(keys)
            if line is None:
                continue
            amount, raw = _amount(group)
            if amount is None:
                amount = 0.0
                unreadable = unreadable or (
                    f"{start}: the group for {'/'.join(map(str, keys))} carries no readable amount "
                    f"in UnblendedCost or BlendedCost (found {raw!r})"
                )
            amounts = actuals.lines.setdefault(line, {})
            # Two pages can carry the same line for the same day. The exporter
            # already paid for both, so the day's spend is their sum.
            amounts[start] = amounts.get(start, 0.0) + amount

    actuals.days = sorted(set(days))
    if results and not days and actuals.error is None and unreadable is None:
        # Rows with no start date are another layout, not an empty export.
        unreadable = (
            "no row in ResultsByTime carries a start date under TimePeriod or Time"
        )
    if unreadable is not None:
        actuals.error = unreadable
    return actuals


def load_actuals(path) -> Actuals:
    """Read an actuals file. A missing or malformed file is a reconcile error."""
    path = Path(path)
    if not path.exists():
        raise ReconcileError(f"actuals file not found: {path}")
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ReconcileError(f"cannot read actuals from {path}: {exc}") from exc
    return parse_actuals(payload)