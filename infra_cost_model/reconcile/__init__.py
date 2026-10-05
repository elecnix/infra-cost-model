"""Reconcile a cost model with an actuals file (#444).

The engine reads a payload an exporter already wrote — the issue's example is
the output of `aws ce get-cost-and-usage` — so the package never calls a cloud
API and a reconciliation can be re-run from a file in a repository.

Six traps the comparison has to survive (#444):

1. Some lines bill once a month and report an explicit ``$0.00`` row for every
   other day. The divisor runs over the days the line was alive for, not the
   one day it charged, so a monthly charge projects at its own size rather than
   at 30.4/7.
2. Cost Explorer omits a group on days it didn't bill. Those days are still
   days the line was alive, so a sparse line projects a full month of its own
   rate instead of three times it.
3. A line that started billing mid-window was alive for fewer days. Its first
   billed day is read from every day the file carries, not just the window, so
   the line projects at its true monthly run-rate. Export more history and the
   divisor reaches the whole window, which is how a user tells "new" from
   "billed $0 before".
4. Several nodes can map to one bill line. Nodes and lines are grouped into
   connected components and each group is compared once.
5. A failed or truncated payload reports ``unreadable``. It never reads as $0.
6. Bill lines the model deliberately leaves out go in an ``unmodelled``
   allowlist, each with a written reason.

One residual follows from traps 1 to 3. A periodic charge whose charge day falls
inside the window was "alive" for the days after it, not for the whole window,
so it projects slightly high — a monthly charge billed on day 3 of a 30-day
window projects about 13% above its own size. Exporting more history removes it:
the wider the file, the more often the first billed day sits at or before the
window start. `--window-days 30` over a file of at least that many days is the
shape the issue recommends, and the per-line `divisorDays` in the report shows
the number each line was projected over.
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