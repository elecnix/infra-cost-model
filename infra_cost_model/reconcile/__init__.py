"""Reconcile a cost model with an actuals file (#444).

The engine reads a payload an exporter already wrote — the issue's example is
the output of `aws ce get-cost-and-usage` — so the package never calls a cloud
API and a reconciliation can be re-run from a file in a repository.

Group the query by ``SERVICE`` and then ``USAGE_TYPE``, in that order, to get
one bill line per usage type. The payload does not record the order, so a group
with two keys is read as ``(service, usage_type)``. A single joined
``SERVICE/USAGE_TYPE`` key is read the same way:

    aws ce get-cost-and-usage ... \\
      --group-by Type=DIMENSION,Key=SERVICE Type=DIMENSION,Key=USAGE_TYPE

Six traps the comparison has to survive (#444):

1. Some lines bill once a month and report an explicit ``$0.00`` row for every
   other day. The divisor runs over the days the line was alive for, not the
   one day it charged, so a monthly charge projects at its own size rather than
   at 30.4/7.
2. Cost Explorer omits a group on days it didn't bill. Those days are still
   days the line was alive, so a sparse line projects a full month of its own
   rate instead of three times it.
3. A line that started billing mid-window was alive for fewer days. A
   billing-shape rule (#462) tells a new line from a rare charge. A line is new
   only if no earlier day in the file billed it and it bills on every day from
   its first billed day on, for at least `--new-line-days` days (default 7).
   It then divides by the days since its first billed day. Any other line
   zero-fills and divides by the whole window, so a quarterly or annual charge
   projects at its share of the window, not at a run-rate.
4. Several nodes can map to one bill line. Nodes and lines are grouped into
   connected components and each group is compared once.
5. A failed or truncated payload reports ``unreadable``. It never reads as $0.
6. Bill lines the model deliberately leaves out go in an ``unmodelled``
   allowlist, each with a written reason.

A periodic charge never counts as new, because it has gaps after its first billed
day. The per-line `divisorDays` and `divisorRule` in the report show the number
and the rule each line was projected with. A window shorter than the charge
period still cannot show the charge at its true size: a monthly charge in a
7-day window projects at 30.4 divided by 7. `--window-days 30` over a file of
at least that many days is the shape the issue recommends.
"""

from infra_cost_model.reconcile.actuals import DAYS_PER_MONTH, Actuals, load_actuals
from infra_cost_model.reconcile.billing import BillLine, node_billing_lines
from infra_cost_model.reconcile.config import (
    ReconcileConfig,
    UnmodelledLine,
    load_config,
)
from infra_cost_model.reconcile.reconcile import (
    BillLineResult,
    Group,
    Reconciliation,
    ReconcileError,
    UnmodelledResult,
    reconcile,
)

__all__ = [
    "DAYS_PER_MONTH",
    "Actuals",
    "BillLine",
    "BillLineResult",
    "Group",
    "ReconcileConfig",
    "ReconcileError",
    "Reconciliation",
    "UnmodelledLine",
    "UnmodelledResult",
    "load_actuals",
    "load_config",
    "node_billing_lines",
    "reconcile",
]