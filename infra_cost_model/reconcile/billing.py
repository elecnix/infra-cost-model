"""Resolve the bill line each node and usage metric lands on (#444, on #442).

#442 gives each node a `billingLines` field mapping a usage metric to the bill
line the charge appears on:

```yaml
billingLines:
  natHours: { provider: aws-cost-explorer, service: "Amazon Virtual Private Cloud", usageType: "USW2-NatGateway-Hours" }
```

The reconciler reads exactly that field. It does not resolve the handler
defaults #442 also adds, so a node without the field is reported as unplaced
money rather than guessed at.
"""

from dataclasses import dataclass
from typing import Optional

from infra_cost_model.reconcile.actuals import BillLineKey, ReconcileError

# The one provider whose bill format this command can read. `reconcile.yaml`
# may allowlist a line from another bill provider once an exporter writes its
# payload in this shape.
SUPPORTED_PROVIDER = "aws-cost-explorer"


@dataclass(frozen=True)
class BillLine:
    """The line one usage metric of one node bills on."""

    metric: str
    provider: str
    service: str
    usage_type: Optional[str]

    @property
    def key(self) -> BillLineKey:
        return BillLineKey(self.service, self.usage_type)

    @property
    def label(self) -> str:
        return self.key.label()


def _line_for(address: str, metric: str, declared: dict) -> BillLine:
    if not isinstance(declared, dict):
        raise ReconcileError(
            f"{address}: billingLines.{metric} must be a mapping with a service"
        )
    service = declared.get("service")
    if not isinstance(service, str) or not service:
        raise ReconcileError(
            f"{address}: billingLines.{metric} needs a service naming the bill line"
        )
    provider = declared.get("provider") or SUPPORTED_PROVIDER
    if provider != SUPPORTED_PROVIDER:
        raise ReconcileError(
            f"{address}: billingLines.{metric} names provider {provider!r}; "
            f"reconcile reads {SUPPORTED_PROVIDER} actuals only"
        )
    usage_type = declared.get("usageType")
    if usage_type is not None and not isinstance(usage_type, str):
        raise ReconcileError(
            f"{address}: billingLines.{metric}.usageType must be a string when present"
        )
    return BillLine(metric=metric, provider=provider, service=service,
                    usage_type=usage_type or None)


def node_billing_lines(address: str, node: dict) -> list[BillLine]:
    """The bill lines a node declares, one per usage metric.

    An empty list means the node declares none, which is a report the caller
    surfaces rather than an error: #442 has not reached every model yet.
    """
    declared = node.get("billingLines")
    if declared is None:
        return []
    if not isinstance(declared, dict):
        raise ReconcileError(f"{address}: billingLines must be a mapping of metric to line")
    return [_line_for(address, metric, line) for metric, line in declared.items()]