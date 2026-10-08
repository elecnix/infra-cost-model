"""Which pools of a metric share one bill, and who prices it (#294).

Most catalog tiers apply to one region's use of a metric, and each region
prices its own rows. A few providers bill a group of pools together: on one
meter or SKU (#389), at one account-wide price (#378), or with one free
allowance that covers several regions (#336), several metrics (#338), or a
few named regions (#402). Asking each of those questions separately means
the engine holds one algorithm per rule and the precedence between them
lives in the order it calls them, so every new rule is another branch and
another place the order can be got wrong.

``billing_scope`` answers both questions at once: which pools of a metric
share a bill, and how that bill is priced. The engine resolves a scope per
pool, groups the pools by it, and prices each group with the one algorithm
the scope names. The precedence is here, in the order the rules are
checked, and the group keys are here, next to the tables that produce
them, so adding a rule means adding a table row rather than editing the
engine.

Which scope wins where the rules overlap:

- a named meter or SKU beats an account-wide price (#389 vs #378), since
  the group's tiers count the group's total;
- an allowance for a few named regions beats an account-wide price
  (#402 vs #378) and beats an allowance shared across metrics (#338);
- an account-wide price beats an allowance shared across metrics (#378 vs
  #338), and an allowance shared across metrics beats an account-wide
  allowance (#338 vs #336), since both state the allowance once for the
  account and the shared table says so more precisely;
- everything else leaves the pool on the region's own rows.

``BILLING_REGION`` is the default: a metric none of the tables names keeps
each region's use on its own rows.
"""

from dataclasses import dataclass
from typing import Hashable, Optional

from infra_cost_model.pricing.free_tiers import (
    ACCOUNT, FREE_ALLOWANCE_REGIONS, free_tier_scope, shared_free_allowance,
)
from infra_cost_model.pricing.global_services import (
    GLOBAL_PRICE_REGIONS, is_global_metric,
)
from infra_cost_model.pricing.price_pools import price_pool

# The pool pays one region's own rows, unless a table below says otherwise.
BILLING_REGION = "region"
# One free allowance for the account's use in every region (#336).
BILLING_ACCOUNT = "account"
# One free allowance for the account's use of several metrics (#338).
BILLING_SHARED = "shared"
# One price for the account's use in every region (#378).
BILLING_GLOBAL = "global"
# One meter or SKU for a group of regions (#389).
BILLING_POOL = "pool"
# One free allowance for the use in a few named regions (#402).
BILLING_FREE_REGIONS = "free-regions"


@dataclass(frozen=True)
class BillingScope:
    """The pools that share one bill, and how that bill is priced.

    ``group`` names the set of pools that share one bill: two pools of the
    same metric bill together when their scopes are equal. ``None`` leaves
    the pool on its own region's rows.

    ``price_regions`` names the regions whose rows price the group's total,
    in order of preference, before the group's own regions are tried (#384).
    ``free_regions`` names the regions of a meter group whose own rows carry
    the group's free allowance, so that the rest of the group pays full
    price (#404). ``allowance`` is the monthly free quantity a group of
    metrics shares, where the table states it rather than the rows (#338).
    """
    kind: str
    group: Optional[Hashable] = None
    price_regions: tuple[str, ...] = ()
    free_regions: tuple[str, ...] = ()
    allowance: Optional[float] = None


def billing_scope(vendor: Optional[str], service: Optional[str],
                  usage_metric: str, region: Optional[str],
                  scaling: tuple = ()) -> BillingScope:
    """Return the scope that decides which pools share one bill.

    ``scaling`` is the pool's tier-scaling parameters: rows with a ``per``
    multiplier move their tier boundaries, so charges pool only when those
    parameters agree (#294). Every scope keeps its pools apart by it, except
    an allowance shared across metrics, which is one account-wide quantity
    whether or not the rows' boundaries move.
    """
    metric = (vendor, service, usage_metric)

    # One meter or SKU for a group of regions (#389). Its rows carry the
    # group's tiers, so it outranks the rules that would bill the regions
    # separately.
    pool = price_pool(vendor, service, usage_metric, region)
    if pool is not None:
        return BillingScope(
            BILLING_POOL, metric + (scaling, pool),
            free_regions=FREE_ALLOWANCE_REGIONS.get(metric, ()))

    # One free allowance for the use in a few named regions, for a metric no
    # meter or SKU bills together (#402). A pool outside those regions has no
    # free tier and keeps its own price.
    free_regions = FREE_ALLOWANCE_REGIONS.get(metric)
    if free_regions is not None and region in free_regions:
        return BillingScope(BILLING_FREE_REGIONS, metric + (scaling,))

    # One price for the account's use in every region (#378). The global and
    # us-east-1 rows price the total, so that a live sync's per-region copies
    # and a node in a region with rows only of its own price the same (#384).
    if is_global_metric(vendor, service, usage_metric):
        return BillingScope(BILLING_GLOBAL, metric + (scaling,),
                            price_regions=GLOBAL_PRICE_REGIONS)

    # One free allowance for the account's use of several metrics (#338).
    shared = shared_free_allowance(vendor, service, usage_metric)
    if shared is not None:
        return BillingScope(BILLING_SHARED, shared, allowance=shared.allowance)

    # One free allowance for the account's use of one metric in every region
    # (#336).
    if free_tier_scope(vendor, service, usage_metric) == ACCOUNT:
        return BillingScope(BILLING_ACCOUNT, metric + (scaling,))

    return BillingScope(BILLING_REGION)


def query_regions(vendor: Optional[str], service: Optional[str],
                  usage_metric: str, region: Optional[str]) -> list:
    """The regions whose rows price one node's quantity, in order of preference.

    A node's own region first, then the regions ``billing_scope`` names for
    its group: a global service's region with no rows of its own reads the
    global or us-east-1 rows (#384). The same scope that groups the pools
    decides this, so the two can't disagree about which rows a metric reads.
    """
    scope = billing_scope(vendor, service, usage_metric, region)
    return [region] + [r for r in scope.price_regions if r != region]
