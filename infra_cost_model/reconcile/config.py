"""Read `reconcile.yaml`, the thresholds and the accepted gaps (#444).

```yaml
warnPct: 15
failPct: 30
unmodelled:
  - service: Tax
    reason: billed as its own line with no per-resource grain
```

Some bill lines are real and deliberately left out of the model. Each one needs
a written reason, so the comparison doesn't stay red over a gap somebody
already decided on, and so the decision stays visible in the repository rather
than in a collector's code.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from infra_cost_model.reconcile.actuals import ReconcileError

DEFAULT_WARN_PCT = 15.0
DEFAULT_FAIL_PCT = 30.0

_KNOWN_KEYS = {"warnPct", "failPct", "unmodelled"}
_KNOWN_ENTRY_KEYS = {"service", "usageType", "reason"}


@dataclass(frozen=True)
class UnmodelledLine:
    """A bill line the model leaves out, and why."""

    service: str
    usage_type: Optional[str]
    reason: str

    def matches(self, service: str, usage_type: Optional[str]) -> bool:
        """An entry without a usage type covers the whole service."""
        return service == self.service and (
            self.usage_type is None or usage_type == self.usage_type
        )


@dataclass
class ReconcileConfig:
    """The optional file beside the model. Defaults apply when there is none."""

    warn_pct: float = DEFAULT_WARN_PCT
    fail_pct: float = DEFAULT_FAIL_PCT
    unmodelled: list[UnmodelledLine] = field(default_factory=list)

    def entry_for(self, service: str, usage_type: Optional[str]) -> Optional[UnmodelledLine]:
        for entry in self.unmodelled:
            if entry.matches(service, usage_type):
                return entry
        return None


def _pct(raw, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ReconcileError(f"reconcile.yaml: {name} must be a number")
    if raw < 0:
        raise ReconcileError(f"reconcile.yaml: {name} cannot be negative")
    return float(raw)


def _entry(index: int, raw: dict) -> UnmodelledLine:
    if not isinstance(raw, dict):
        raise ReconcileError(f"reconcile.yaml: unmodelled[{index}] must be a mapping")
    unknown = set(raw) - _KNOWN_ENTRY_KEYS
    if unknown:
        raise ReconcileError(
            f"reconcile.yaml: unmodelled[{index}] has unknown keys: "
            + ", ".join(sorted(unknown))
        )
    service = raw.get("service")
    if not isinstance(service, str) or not service:
        raise ReconcileError(f"reconcile.yaml: unmodelled[{index}] needs a service")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ReconcileError(
            f"reconcile.yaml: unmodelled entry for {service} needs a written reason"
        )
    usage_type = raw.get("usageType")
    if usage_type is not None and not isinstance(usage_type, str):
        raise ReconcileError(
            f"reconcile.yaml: unmodelled entry for {service} needs a string usageType"
        )
    return UnmodelledLine(service=service, usage_type=usage_type or None,
                          reason=reason.strip())


def load_config(path) -> ReconcileConfig:
    """Read a `reconcile.yaml`. A missing path yields the defaults."""
    if path is None:
        return ReconcileConfig()
    path = Path(path)
    if not path.exists():
        raise ReconcileError(f"reconcile config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ReconcileError(f"cannot read {path}: {exc}") from exc
    if raw is None:
        return ReconcileConfig()
    if not isinstance(raw, dict):
        raise ReconcileError(f"{path}: expected a mapping of settings")

    unknown = set(raw) - _KNOWN_KEYS
    if unknown:
        raise ReconcileError(
            f"reconcile.yaml: unknown keys: " + ", ".join(sorted(unknown))
        )

    entries = [_entry(index, item)
               for index, item in enumerate(raw.get("unmodelled") or [])]
    seen = {}
    for entry in entries:
        marker = (entry.service, entry.usage_type)
        if marker in seen:
            raise ReconcileError(
                f"reconcile.yaml: two unmodelled entries for {entry.service}"
            )
        seen[marker] = entry

    config = ReconcileConfig(
        warn_pct=_pct(raw.get("warnPct", DEFAULT_WARN_PCT), "warnPct"),
        fail_pct=_pct(raw.get("failPct", DEFAULT_FAIL_PCT), "failPct"),
        unmodelled=entries,
    )
    if config.fail_pct < config.warn_pct:
        raise ReconcileError(
            f"reconcile.yaml: failPct ({config.fail_pct:g}) is below "
            f"warnPct ({config.warn_pct:g})"
        )
    return config