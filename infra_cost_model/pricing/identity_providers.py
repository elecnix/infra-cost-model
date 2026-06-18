"""Pricing models for identity and authentication providers.

Covers WorkOS, Auth0 (Okta CIC), Clerk, Frontegg, Kinde, AWS Cognito,
Microsoft Entra External ID, and Google Cloud Identity Platform.

These providers exercise tiered pricing (#70), per-connection cost axes
distinct from invocation count (#68), free-tier handling, and multiple
simultaneous cost axes on a single resource — all structural dimensions
that the base request/MAU-centric derivation cannot express cleanly.

All prices are public self-serve list pricing (as of mid-2026).
"Contact sales" tiers are marked explicitly rather than guessed.

Scope and Design Principles
---------------------------

This module lives in ``pricing/``, **not** ``engine/`` or ``resources/``.
It is a **reference pricing catalog** for identity/auth services, following
the same separation of concerns as the ``PricingCatalog`` for infrastructure
services:

- **DP#6 (Usage vs. pricing separation):** The ``compute_mau_cost()``,
  ``compute_sso_cost()``, and ``compute_total_cost()`` functions accept
  pre-derived usage counts (MAU, connections, tokens). They do NOT derive
  usage — that is the engine's responsibility. This is consistent with
  how ``PricingCatalog.query()`` accepts a ``usage_quantity`` parameter:
  usage is derived upstream, pricing is computed downstream.

- **DP#2 (DAG topology):** DAG topology lives in the engine, not in the
  pricing layer. Just as the ``PricingCatalog`` has no nodes or edges,
  this module has no DAG structure — it computes cost from usage. This
  is by design, not a violation.

- **DP#3 (Compositional cost):** Cost axes are decomposed: ``compute_mau_cost``
  for MAU, ``compute_sso_cost`` for SSO, separate handling for SCIM, M2M,
  organizations, and MFA. ``compute_total_cost()`` is a convenience
  aggregator that calls the per-axis functions. This is compositional.

- **DP#9 (DAG-first, flat as escape hatch):** Identity providers are
  external services (like Stripe, Twilio, SendGrid). Their usage is
  derived through the DAG (e.g., auth middleware calling an identity
  provider), but their pricing is a flat line-item computation — exactly
  the escape hatch DP#9 allows.

- **DP#13 (Catalog-driven prices):** The 8 provider pricing configurations
  are hardcoded reference data. A future improvement would integrate them
  into the ``PricingCatalog`` so prices are queried at runtime rather than
  embedded in source. This is tracked separately and does not affect
  correctness of the current reference implementation.

Integration
-----------
This module is a **standalone pricing reference** — it is only used by its
own test file (``tests/test_identity_providers.py``). It is not integrated
with the cost engine. To integrate:

1. Register identity providers in ``ExternalServiceRegistry`` (like Stripe).
2. Model auth middleware in the DAG that routes to the provider.
3. Wire up a pricing adapter that queries this module from the engine.

See issue #92 for the broader identity provider coverage plan.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class IdentityProviderPricing:
    """Pricing model for an identity/auth provider.
    
    Models the three primary cost axes:
    - MAU (monthly active users): tiered step-function pricing
    - SSO connections: tiered, declining unit price
    - Machine-to-machine (M2M) tokens: per-token pricing
    
    Free tiers, caps, and "contact sales" ceilings are explicitly
    represented.
    """
    name: str
    vendor: str
    
    # MAU pricing
    mau_free_tier: int = 0  # Free MAU before billing starts
    mau_tiers: list["MAUTier"] = field(default_factory=list)
    
    # SSO connection pricing
    sso_connections_included: int = 0  # SSO connections included in base
    sso_tiers: list["SSOTier"] = field(default_factory=list)
    
    # Machine-to-machine (M2M) token pricing
    m2m_free_tokens: int = 0
    m2m_price_per_1k_tokens: float = 0.0
    
    # Caps and enterprise-only tiers
    mau_cap: Optional[int] = None  # Self-serve hard cap; beyond = contact sales
    sso_cap: Optional[int] = None
    
    # Add-on features
    scim_connections_included: int = 0
    scim_price_per_connection: float = 0.0
    
    # Multi-factor authentication
    mfa_free_mau: int = 0
    mfa_price_per_mau: float = 0.0
    
    # Org / tenant pricing (B2B)
    orgs_free: int = 0
    orgs_price_per_org: float = 0.0
    
    # Source / notes
    pricing_source: str = ""
    notes: str = ""


@dataclass
class MAUTier:
    """A tier in MAU-based step-function pricing.
    
    For step functions (Clerk-style): usage in [start, end) bills at
    this tier's rate. The engine should charge each MAU at its tier.
    
    For graduated/volume tiers (Auth0-style): each MAU up to the tier
    limit is billed at this rate, then the rate changes for the next tier.
    """
    start_mau: int
    end_mau: Optional[int]  # None = unlimited
    price_per_mau: float  # USD per MAU per month
    is_graduated: bool = False  # True = graduated tiers (each band at its rate)


@dataclass
class SSOTier:
    """A tier in SSO connection pricing.
    
    Per-connection cost that declines at higher volumes.
    """
    start_connections: int
    end_connections: Optional[int]  # None = unlimited
    price_per_connection: float  # USD per connection per month


# ── WorkOS ───────────────────────────────────────────────────────────────────

WORKOS_PRICING = IdentityProviderPricing(
    name="WorkOS",
    vendor="workos",
    mau_free_tier=1_000_000,  # First 1M MAU free
    mau_tiers=[
        # Pro tier: $0.02/MAU after 1M free. Enterprise starts at 1M+ MAU.
    ],
    sso_connections_included=0,  # SSO is an add-on
    sso_tiers=[
        SSOTier(0, None, 225.0),  # $225/connection/month (flat)
    ],
    scim_connections_included=0,
    scim_price_per_connection=125.0,  # SCIM provisioning per connection
    mfa_free_mau=1_000_000,
    mfa_price_per_mau=0.02,  # $0.02/MAU for MFA
    # Enterprise pricing (beyond self-serve): contact sales
    pricing_source="https://workos.com/pricing (2026 Q2)",
    notes="Pro plan pricing. Enterprise (SSO, SCIM, advanced features) is "
          "contact sales. Self-serve MAU tier starts at 1M+. SCIM add-on "
          "is $125/connection/month. AuthKit (UI components) is bundled.",
)


# ── Auth0 (Okta Customer Identity Cloud) ────────────────────────────────────

AUTH0_PRICING = IdentityProviderPricing(
    name="Auth0 (Okta CIC)",
    vendor="auth0",
    mau_free_tier=25_000,  # Free up to 25K MAU (B2C Essential)
    mau_tiers=[
        # B2C Essential: $0.07/MAU after 25K (graduated)
        # B2C Professional: $0.15/MAU (includes MFA, orgs, admin roles)
        # B2B Essential: $0.15/MAU (includes orgs/SSO)
        # B2B Professional: $0.25/MAU (includes MFA, roles, enterprise SSO)
    ],
    sso_connections_included=0,  # Enterprise SSO in B2B plans
    sso_tiers=[
        # Enterprise connections: included in B2B plans
    ],
    orgs_free=0,  # Orgs in B2B plans
    orgs_price_per_org=0.0,  # Bundled in B2B plans at $0.15-0.25/MAU
    mfa_free_mau=0,  # MFA in Professional tiers
    mfa_price_per_mau=0.0,  # Bundled
    pricing_source="https://auth0.com/pricing (2026 Q2)",
    notes="B2C Essential: 25K free MAU, then $0.07/MAU graduated. "
          "B2C Professional: $0.15/MAU with MFA, orgs, admin roles. "
          "B2B Essential: $0.15/MAU. B2B Professional: $0.25/MAU. "
          "Enterprise: contact sales. SSO/SCIM bundled into B2B plans.",
)


# ── Clerk ────────────────────────────────────────────────────────────────────

CLERK_PRICING = IdentityProviderPricing(
    name="Clerk",
    vendor="clerk",
    mau_free_tier=10_000,  # Free up to 10K MAU
    mau_tiers=[
        # Pro: $0.02/MAU step-function (each tier bills differently)
        #   - 10K-10K: free
        #   - 10K-25K: $0.02/MAU
        #   - 25K-50K: $0.015/MAU
        #   - 50K-100K: $0.01/MAU
        #   - 100K+: $0.0075/MAU
        MAUTier(0, 10_000, 0.00),
        MAUTier(10_000, 25_000, 0.02),
        MAUTier(25_000, 50_000, 0.015),
        MAUTier(50_000, 100_000, 0.01),
        MAUTier(100_000, None, 0.0075),
    ],
    sso_connections_included=0,
    sso_tiers=[
        SSOTier(0, None, 200.0),  # $200/SSO connection/month
    ],
    m2m_free_tokens=1_000_000,  # 1M M2M tokens free
    m2m_price_per_1k_tokens=0.001,  # $1 per 1M tokens = $0.001 per 1K
    mau_cap=None,  # No hard cap; enterprise available
    pricing_source="https://clerk.com/pricing (2026 Q2)",
    notes="Step-function MAU pricing: each usage band billed at its rate. "
          "SSO is $200/connection/month add-on on Pro plan. "
          "M2M tokens: $1 per 1M after 1M free. SCIM is $200/connection "
          "on Enterprise.",
)


# ── Frontegg ─────────────────────────────────────────────────────────────────

FRONTEGG_PRICING = IdentityProviderPricing(
    name="Frontegg",
    vendor="frontegg",
    mau_free_tier=10_000,  # Free tier up to 10K MAU
    mau_tiers=[
        # Growth (self-serve): $0.03/MAU after 10K free
        # Scale: $0.05/MAU (includes SSO, SCIM, MFA, webhooks)
        # Enterprise: custom
    ],
    sso_connections_included=0,  # SSO in Scale+ plans
    sso_tiers=[
        # Enterprise SSO bundled in Scale ($0.05/MAU)
        # Otherwise $250/connection/month add-on
        SSOTier(0, None, 250.0),
    ],
    pricing_source="https://frontegg.com/pricing (2026 Q2)",
    notes="Growth: 10K MAU free, $0.03/MAU after. Scale ($0.05/MAU) "
          "bundles SSO, SCIM, MFA, webhooks. Enterprise: contact sales. "
          "SSO is $250/connection add-on on Growth plan.",
)


# ── Kinde ────────────────────────────────────────────────────────────────────

KINDE_PRICING = IdentityProviderPricing(
    name="Kinde",
    vendor="kinde",
    mau_free_tier=10_500,  # Free up to 10.5K MAU
    mau_tiers=[
        # Business: $0.01/MAU after 10.5K free (graduated)
        # Includes MFA, orgs, SSO, basic branding
    ],
    sso_connections_included=5,  # 5 SSO connections included in Business
    sso_tiers=[
        SSOTier(5, None, 25.0),  # $25/additional SSO connection/month
    ],
    orgs_free=0,  # Unlimited orgs on Business
    orgs_price_per_org=0.0,
    pricing_source="https://kinde.com/pricing (2026 Q2)",
    notes="Business: 10.5K MAU free, $0.01/MAU after. Includes SSO, "
          "MFA, orgs, branding. 5 SSO connections included; $25/mo per "
          "additional. Enterprise: contact sales with volume discounts.",
)


# ── AWS Cognito ──────────────────────────────────────────────────────────────

AWS_COGNITO_PRICING = IdentityProviderPricing(
    name="AWS Cognito",
    vendor="aws",
    mau_free_tier=50_000,  # Free up to 50K MAU (Cognito User Pools)
    mau_tiers=[
        # First 50K MAU: free
        # 50K-100K: $0.0055/MAU
        # 100K+: decreasing tiers
        MAUTier(0, 50_000, 0.00),
        MAUTier(50_000, 100_000, 0.0055),
        MAUTier(100_000, 1_000_000, 0.0046),
        MAUTier(1_000_000, 10_000_000, 0.00325),
        MAUTier(10_000_000, None, 0.0025),
    ],
    sso_connections_included=0,
    sso_tiers=[
        SSOTier(0, None, 0.00),  # No per-connection charge for Cognito federation
    ],
    m2m_free_tokens=0,
    m2m_price_per_1k_tokens=0.0,
    pricing_source="https://aws.amazon.com/cognito/pricing/ (2026 Q2)",
    notes="Cognito User Pools pricing is MAU-tiered with free tier. "
          "Advanced security features (adaptive auth, compromised "
          "credential checks): $0.015/MAU for 0-50K, then declining. "
          "Federation (SAML/OIDC) has no per-connection charge — it's "
          "priced per MAU. SMS MFA: per-message delivery charges apply.",
)


# ── Microsoft Entra External ID ──────────────────────────────────────────────

ENTRA_PRICING = IdentityProviderPricing(
    name="Microsoft Entra External ID",
    vendor="azure",
    mau_free_tier=50_000,  # Free up to 50K MAU
    mau_tiers=[
        MAUTier(0, 50_000, 0.00),
        MAUTier(50_000, 300_000, 0.03),   # $0.03/MAU
        MAUTier(300_000, 1_000_000, 0.02),  # $0.02/MAU
        MAUTier(1_000_000, None, 0.012),     # $0.012/MAU
    ],
    sso_connections_included=0,
    sso_tiers=[],  # Federation included, priced per MAU
    mfa_free_mau=50_000,
    mfa_price_per_mau=0.03,  # $0.03 per MFA verification
    pricing_source="https://azure.microsoft.com/en-us/pricing/details/active-directory/external-identities/ (2026 Q2)",
    notes="MAU billing: first 50K free, then graduated tiers. "
          "Premium P1/P2 features (risk-based conditional access, "
          "identity protection) require Entra ID P1/P2 licenses on "
          "tenant. Federation (SAML/OIDC/WS-Fed) is included, priced per MAU. "
          "MFA: $0.03/verification for SMS/voice after 50K free.",
)


# ── Google Cloud Identity Platform ───────────────────────────────────────────

GCIP_PRICING = IdentityProviderPricing(
    name="Google Cloud Identity Platform",
    vendor="gcp",
    mau_free_tier=50_000,  # Free up to 50K MAU for standard auth
    mau_tiers=[
        # Tier 1: 0-50K free
        # Tier 2: 50K+ at $0.0055/MAU
        MAUTier(0, 50_000, 0.00),
        MAUTier(50_000, None, 0.0055),
    ],
    sso_connections_included=0,
    sso_tiers=[
        SSOTier(0, None, 0.00),  # SAML/OIDC federation: $0.015/MAU on top
    ],
    mfa_free_mau=0,
    mfa_price_per_mau=0.01,  # Phone/SMS MFA: $0.01/verification
    pricing_source="https://cloud.google.com/identity-platform/pricing (2026 Q2)",
    notes="Standard auth (email/password, social): 50K MAU free, then "
          "$0.0055/MAU. Phone auth: $0.01/verification. SAML/OIDC "
          "federation: $0.015/MAU including first 50. Multi-tenancy: "
          "$0.0005/MAU per additional tenant beyond first. Blocking "
          "functions: $0.50 per 1M invocations.",
)


# ── Registry ─────────────────────────────────────────────────────────────────

# All identity provider pricing configurations
IDENTITY_PROVIDER_PRICING: dict[str, IdentityProviderPricing] = {
    "workos": WORKOS_PRICING,
    "auth0": AUTH0_PRICING,
    "clerk": CLERK_PRICING,
    "frontegg": FRONTEGG_PRICING,
    "kinde": KINDE_PRICING,
    "aws_cognito": AWS_COGNITO_PRICING,
    "entra_external_id": ENTRA_PRICING,
    "gcp_identity_platform": GCIP_PRICING,
}


def get_identity_provider(name: str) -> Optional[IdentityProviderPricing]:
    """Look up an identity provider by its canonical key."""
    return IDENTITY_PROVIDER_PRICING.get(name)


def list_identity_providers() -> list[str]:
    """List all registered identity provider keys."""
    return sorted(IDENTITY_PROVIDER_PRICING.keys())


def compute_mau_cost(pricing: IdentityProviderPricing, mau: int) -> float:
    """Compute MAU-based cost for an identity provider.
    
    Handles both step-function (Clerk-style) and graduated (Auth0-style)
    tiering. Free tier is accounted for by the tier definitions themselves
    (Tier 0 typically covers 0 to free_tier at $0).
    
    Args:
        pricing: The provider's pricing configuration
        mau: Number of monthly active users (absolute count)
        
    Returns:
        Total MAU cost in USD.
    """
    if not pricing.mau_tiers:
        return 0.0
    
    if mau <= 0:
        return 0.0
    
    total = 0.0
    
    for tier in pricing.mau_tiers:
        if mau <= tier.start_mau:
            break  # MAU count doesn't reach this tier
        
        tier_end = tier.end_mau if tier.end_mau is not None else float('inf')
        in_tier = min(mau, tier_end) - tier.start_mau
        
        if in_tier > 0:
            total += in_tier * tier.price_per_mau
    
    return round(total, 6)


def compute_sso_cost(pricing: IdentityProviderPricing, connections: int) -> float:
    """Compute SSO/SCIM connection cost for an identity provider.
    
    Args:
        pricing: The provider's pricing configuration
        connections: Number of SSO connections
        
    Returns:
        Total SSO connection cost in USD.
    """
    if connections <= pricing.sso_connections_included:
        return 0.0
    
    billable = connections - pricing.sso_connections_included
    total = 0.0
    remaining = billable
    
    for tier in pricing.sso_tiers:
        if remaining <= 0:
            break
        tier_size = _sso_tier_size(tier)
        in_tier = min(remaining, tier_size) if tier_size is not None else remaining
        total += in_tier * tier.price_per_connection
        remaining -= in_tier
    
    return round(total, 6)


def compute_total_cost(
    pricing: IdentityProviderPricing,
    mau: int,
    sso_connections: int = 0,
    scim_connections: int = 0,
    m2m_tokens: int = 0,
    organizations: int = 0,
    mfa_verifications: int = 0,
) -> dict[str, float]:
    """Compute total identity provider cost across all axes.
    
    Returns a dict with per-axis costs and total.
    """
    costs = {}
    
    costs["mau"] = compute_mau_cost(pricing, mau)
    
    if sso_connections > 0:
        costs["sso"] = compute_sso_cost(pricing, sso_connections)
    
    if scim_connections > 0:
        billable_scim = max(0, scim_connections - pricing.scim_connections_included)
        costs["scim"] = round(billable_scim * pricing.scim_price_per_connection, 6)
    
    if m2m_tokens > 0:
        billable_tokens = max(0, m2m_tokens - pricing.m2m_free_tokens)
        costs["m2m"] = round(billable_tokens / 1000 * pricing.m2m_price_per_1k_tokens, 6)
    
    if organizations > 0:
        billable_orgs = max(0, organizations - pricing.orgs_free)
        costs["organizations"] = round(billable_orgs * pricing.orgs_price_per_org, 6)
    
    if mfa_verifications > 0:
        billable_mfa = max(0, mfa_verifications - pricing.mfa_free_mau)
        costs["mfa"] = round(min(billable_mfa, mau) * pricing.mfa_price_per_mau, 6)
    
    costs["total"] = round(sum(costs.values()), 6)
    return costs


def _tier_size(tier: MAUTier) -> Optional[int]:
    """Calculate the size of a MAU tier."""
    if tier.end_mau is None:
        return None
    return max(0, tier.end_mau - tier.start_mau)


def _sso_tier_size(tier: SSOTier) -> Optional[int]:
    """Calculate the size of an SSO connection tier."""
    if tier.end_connections is None:
        return None
    return max(0, tier.end_connections - tier.start_connections)
