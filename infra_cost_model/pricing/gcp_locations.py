"""GCP locations that the catalog prices differently from a region.

Plain data, shared by the resource handlers that extract a GCP resource and
the price source that syncs its rows, so an extractor reads these tables
without loading a price source's client.
"""

# Synced GCP regions whose catalog holds no 1st gen Cloud Run functions row
# (Infracost Cloud Pricing API, check date 2026-09-24). A
# `google_cloudfunctions_function` in one of them has unpriced CPU and memory
# usage (#375, #400), so the handler warns at extraction and points at the 2nd
# gen resource, which is priced at Cloud Run rates.
#
# The set was derived from the provider's published region list, not read back
# from a query: the credential the check needs was unavailable, so membership
# is unverified in both directions. Treat a member as "we expect no rows" and
# a non-member as unknown, not as proof that the region is supported. A
# maintainer with a working credential should confirm the membership before
# trusting it, and should widen or drop the set as the provider changes.
FUNCTIONS_GEN1_UNPRICED_REGIONS = frozenset({
    "africa-south1", "asia-south2", "australia-southeast2", "europe-southwest1",
    "europe-west8", "europe-west9", "europe-west10", "europe-west12",
    "me-central1", "me-central2", "me-west1", "northamerica-northeast2",
    "southamerica-west1", "us-south1",
})

# The Cloud Storage locations that are not GCP regions (#397). A bucket's
# location is the catalog region: `us`, `eu` and `asia` for a multi-region,
# and the code of a pair of regions for a dual-region. Cloud Storage
# defines six predefined dual-regions, asia1, eur4, eur5, eur7, eur8 and
# nam4 (https://cloud.google.com/storage/docs/locations). A location that is
# none of these has no rows. A configurable dual-region shares its location
# code with a multi-region, so a bucket's `location` reads as that
# multi-region; the dual-region rows price the code on their own.
GCS_MULTI_REGIONS = ("us", "eu", "asia")
GCS_DUAL_REGIONS = ("nam4", "eur4", "eur5", "eur7", "eur8", "asia1")
GCS_LOCATIONS = GCS_MULTI_REGIONS + GCS_DUAL_REGIONS
