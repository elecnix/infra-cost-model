"""Which pools share one bill, and which rule prices them.

Most metrics bill each region's use on its own. A few providers bill a
group of pools together: on one meter or SKU (#389), at one account-wide
price (#378), or with one free allowance that covers several regions
(#336), several metrics (#338), or a few named regions (#402).
``billing_scope`` answers both questions at once, so a caller asks once
instead of testing each rule in turn.
"""

from infra_cost_model.pricing.billing_scope import (
    BILLING_ACCOUNT, BILLING_FREE_REGIONS, BILLING_GLOBAL, BILLING_POOL,
    BILLING_REGION, BILLING_SHARED, billing_scope,
)
from infra_cost_model.pricing.free_tiers import ACCOUNT, free_tier_scope
from infra_cost_model.pricing.global_services import GLOBAL_PRICE_REGIONS
from infra_cost_model.pricing.price_pools import price_pool

SQS_STANDARD = ("aws", "AmazonSQS", "SQS-Standard-Request")
SQS_FIFO = ("aws", "AmazonSQS", "SQS-FIFO-Request")
DATA_TRANSFER = ("aws", "AWSDataTransfer", "DataTransfer-Internet-Out-GB")
LAMBDA_DURATION = ("aws", "AWSLambda", "Lambda-GB-Second")
ROUTE53_QUERIES = ("aws", "AmazonRoute53", "Route53-Query")
GCS_EGRESS = ("gcp", "CloudStorage", "GCS-Internet-Egress-GiB")
GCS_STORAGE = ("gcp", "CloudStorage", "GCS-Standard-GiB-Month")
S3_STORAGE = ("aws", "AmazonS3", "S3-Standard-Storage")
AZURE_EGRESS = ("azure", "Bandwidth", "Bandwidth-Internet-Out-GB")


def scope_of(metric, region, scaling=()):
    return billing_scope(*metric, region, scaling)


class TestTheDefaultScope:
    def test_a_metric_with_no_rule_keeps_its_own_rows(self):
        assert scope_of(S3_STORAGE, "us-east-1").kind == BILLING_REGION

    def test_the_default_scope_names_no_group(self):
        # A region scope must never put two pools in one bill: each prices
        # from its own rows, so grouping them would price one metric's
        # quantity at another's rate.
        assert scope_of(S3_STORAGE, "us-east-1").group is None

    def test_two_unruled_pools_never_share_a_scope_key(self):
        # Every region-scoped pool has the same empty scope, so the engine
        # has to leave the whole scope alone rather than price the pools it
        # collects as one group.
        assert scope_of(S3_STORAGE, "us-east-1") == scope_of(
            ("gcp", "BigQuery", "BQ-Bytes-Processed"), "us-central1")


class TestAnAccountWideAllowance:
    def test_an_allowance_covers_every_region(self):
        # "The first 100 GB a month ... calculated across all Regions".
        assert scope_of(DATA_TRANSFER, "us-east-1").kind == BILLING_ACCOUNT

    def test_every_region_of_the_metric_shares_one_bill(self):
        assert (scope_of(DATA_TRANSFER, "us-east-1").group
                == scope_of(DATA_TRANSFER, "eu-west-1").group)

    def test_the_metric_is_part_of_the_group_key(self):
        # Lambda duration is account-wide too, but the two allowances are
        # one allowance each.
        assert (scope_of(DATA_TRANSFER, "us-east-1").group
                != scope_of(LAMBDA_DURATION, "us-east-1").group)

    def test_rows_that_scale_differently_do_not_share_a_bill(self):
        # A tier whose bounds a parameter moves applies to one set of
        # parameter values, so two such rows are two bills (#294).
        assert (scope_of(DATA_TRANSFER, "us-east-1", (("Tier", 1),)).group
                != scope_of(DATA_TRANSFER, "us-east-1", (("Tier", 2),)).group)

    def test_the_table_still_answers_for_a_metric(self):
        assert free_tier_scope(*DATA_TRANSFER) == ACCOUNT


class TestAnAllowanceSharedAcrossMetrics:
    def test_one_allowance_covers_both_queue_types(self):
        # AWS gives 1,000,000 free requests a month to standard and FIFO
        # queues together, in every region (#338).
        assert scope_of(SQS_STANDARD, "us-east-1").kind == BILLING_SHARED

    def test_both_queue_types_in_both_regions_share_one_bill(self):
        assert (scope_of(SQS_STANDARD, "us-east-1").group
                == scope_of(SQS_FIFO, "eu-west-1").group)

    def test_another_service_gets_another_allowance(self):
        assert (scope_of(SQS_STANDARD, "us-east-1").group
                != scope_of(("aws", "AmazonCloudFront",
                             "CloudFront-HTTP-Request"), "us-east-1").group)

    def test_the_allowance_is_stated_by_the_table(self):
        # The allowance is the table's own quantity, not one the rows state,
        # so the scope has to carry it for a caller to price the group.
        assert scope_of(SQS_STANDARD, "us-east-1").allowance is not None

    def test_the_allowance_covers_rows_that_scale_differently(self):
        # The allowance is a flat account-wide quantity, not a tier, so it
        # does not split when the rows' bounds move.
        assert (scope_of(SQS_STANDARD, "us-east-1", (("Tier", 1),)).group
                == scope_of(SQS_STANDARD, "us-east-1", (("Tier", 2),)).group)


class TestAnAccountWidePrice:
    def test_a_global_service_bills_the_account_once(self):
        assert scope_of(ROUTE53_QUERIES, "us-east-1").kind == BILLING_GLOBAL

    def test_every_region_of_the_metric_shares_one_bill(self):
        assert (scope_of(ROUTE53_QUERIES, "us-east-1").group
                == scope_of(ROUTE53_QUERIES, "eu-west-1").group)

    def test_the_scope_names_the_rows_that_price_the_total(self):
        # A live sync stores Route 53 rows under each sync region (#361), so
        # the global and us-east-1 rows price the total first (#384).
        assert (scope_of(ROUTE53_QUERIES, "us-east-1").price_regions
                == GLOBAL_PRICE_REGIONS)

    def test_a_meter_group_names_no_other_rows(self):
        assert scope_of(GCS_EGRESS, "us-central1").price_regions == ()


class TestAMeterOrSkuGroup:
    def test_a_zone_is_one_group(self):
        assert scope_of(AZURE_EGRESS, "eastus").kind == BILLING_POOL

    def test_every_zone_of_the_meter_shares_one_bill(self):
        assert (scope_of(AZURE_EGRESS, "eastus").group
                == scope_of(AZURE_EGRESS, "westus2").group)

    def test_another_zone_is_another_group(self):
        assert (scope_of(AZURE_EGRESS, "eastus").group
                != scope_of(AZURE_EGRESS, "japaneast").group)

    def test_a_region_with_a_meter_of_its_own_shares_no_bill(self):
        # The API gives newer regions, such as austriaeast, their own meter.
        assert scope_of(AZURE_EGRESS, "austriaeast").group is None
        assert price_pool(*AZURE_EGRESS, "austriaeast") is None


class TestAFreeAllowanceForNamedRegions:
    def test_a_free_region_gets_the_shared_allowance(self):
        assert scope_of(GCS_STORAGE, "us-central1").kind == BILLING_FREE_REGIONS

    def test_every_free_region_shares_one_bill(self):
        assert (scope_of(GCS_STORAGE, "us-central1").group
                == scope_of(GCS_STORAGE, "us-east1").group)

    def test_another_region_keeps_its_own_price(self):
        # "Always Free quotas apply to usage in US-WEST1, US-CENTRAL1, and
        # US-EAST1 regions", so elsewhere the rows state no free tier.
        assert scope_of(GCS_STORAGE, "europe-west1").group is None


class TestPrecedence:
    def test_a_shared_meter_beats_a_free_allowance_for_named_regions(self):
        # Cloud Storage bills egress from every region on one SKU, but gives
        # its free 100 GiB to three US regions only (#404). The SKU counts
        # the group's tiers, so the egress is a meter group and not a group
        # of the three free regions.
        egress = scope_of(GCS_EGRESS, "us-central1")
        assert egress.kind == BILLING_POOL
        assert egress.free_regions == ("us-central1", "us-east1", "us-west1")

    def test_an_allowance_for_named_regions_names_no_free_regions(self):
        # Those regions are the group, so there is nothing left to mark.
        assert scope_of(GCS_STORAGE, "us-central1").free_regions == ()

    def test_an_account_wide_allowance_is_not_also_a_shared_one(self):
        # #338 states the allowance across metrics and wins over #336, so a
        # metric that is only account-wide must not report as shared.
        assert scope_of(DATA_TRANSFER, "us-east-1").kind == BILLING_ACCOUNT
        assert scope_of(DATA_TRANSFER, "us-east-1").group is not None


class TestTheScopeAnswersTheWholeQuestion:
    def test_one_call_settles_whether_two_pools_share_a_bill(self):
        # The engine asks once per pool and buckets by the answer, so the
        # scope has to name the group without a second lookup.
        east = scope_of(DATA_TRANSFER, "us-east-1")
        west = scope_of(DATA_TRANSFER, "eu-west-1")
        assert east.kind == west.kind
        assert east.group == west.group

    def test_a_metric_the_tables_do_not_name_is_still_scoped(self):
        # An unknown vendor, service or metric gets the region scope rather
        # than an error, so a new pricing row needs no engine change.
        assert billing_scope("acme", "Widgets", "Widget-Request",
                             "nowhere").kind == BILLING_REGION

    def test_a_missing_region_is_still_scoped(self):
        assert scope_of(DATA_TRANSFER, None).kind == BILLING_ACCOUNT
        assert scope_of(GCS_STORAGE, None).group is None

    def test_a_missing_vendor_is_still_scoped(self):
        assert billing_scope(None, None, "M", None).kind == BILLING_REGION


class TestTheScopeIsHashable:
    def test_two_pools_of_one_bill_are_the_same_key(self):
        # The engine buckets pools by scope, so a scope has to be usable as
        # a dictionary key.
        assert len({scope_of(DATA_TRANSFER, "us-east-1"),
                    scope_of(DATA_TRANSFER, "eu-west-1")}) == 1

    def test_two_pools_of_different_bills_are_different_keys(self):
        assert len({scope_of(DATA_TRANSFER, "us-east-1"),
                    scope_of(SQS_STANDARD, "us-east-1")}) == 2


class TestThePricingTablesAreUntouched:
    def test_free_tier_scope_still_answers_for_a_metric(self):
        assert free_tier_scope(*DATA_TRANSFER) == ACCOUNT

    def test_price_pool_still_answers_for_a_region(self):
        assert price_pool(*AZURE_EGRESS, "eastus") is not None
