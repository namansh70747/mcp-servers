"""Pluggable provider registry for email-finding and verification.

Every data source (API, OSS engine, search engine, scraper) is a Provider object with a
uniform interface.  Adding a new source = one entry, no orchestrator changes.

Usage:
    from mcp_base.providers import REGISTRY, ProviderKind

    # Get all configured verifiers ordered cheapest-first
    verifiers = REGISTRY.ordered(kind=ProviderKind.VERIFY)

    # Call a provider (quota-gated)
    result = REGISTRY.call("hunter_verify", email="a@b.com")
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ProviderKind(str, Enum):
    VERIFY  = "verify"    # email deliverability verification
    FIND    = "find"      # email discovery/finder
    SEARCH  = "search"    # web search engine
    HARVEST = "harvest"   # direct site scraping / harvesting
    OSINT   = "osint"     # OSINT aggregator (theHarvester, holehe, …)
    PEOPLE  = "people"    # person/identity resolution


class ProviderCost(str, Enum):
    FREE       = "free"       # keyless, unlimited
    FREE_TIER  = "free-tier"  # requires key, has monthly cap
    PAID       = "paid"       # skip unless explicitly enabled


@dataclass
class Provider:
    name: str
    kind: ProviderKind
    cost: ProviderCost
    priority: int           # lower = tried first (0 = highest priority)
    call_fn: Callable       # call_fn(**kwargs) → dict
    env_keys: list[str] = field(default_factory=list)  # env vars that must be set
    optional_deps: list[str] = field(default_factory=list)  # pip packages
    description: str = ""

    def is_configured(self) -> bool:
        """True if all required env keys are set and optional deps are importable."""
        for k in self.env_keys:
            if not os.environ.get(k, "").strip():
                return False
        for dep in self.optional_deps:
            try:
                __import__(dep.replace("-", "_").split("[")[0])
            except ImportError:
                return False
        return True

    def call(self, **kwargs: Any) -> dict:
        """Invoke the provider.  Returns {ok, ...} or {ok: False, error, skip}."""
        try:
            return self.call_fn(**kwargs)
        except Exception as e:
            return {"ok": False, "error": str(e), "provider": self.name}

    def classify_error(self, resp: dict) -> str:
        """Return "quota" | "rate_limit" | "not_found" | "ok" from a response dict."""
        if not resp.get("ok"):
            err = str(resp.get("error", "")).lower()
            if any(x in err for x in ("quota", "limit", "402", "exhausted")):
                return "quota"
            if "429" in err or "rate" in err:
                return "rate_limit"
        return "ok"


class ProviderRegistry:
    """Central catalogue of all providers, with ordered access and quota-gating."""

    def __init__(self):
        self._providers: list[Provider] = []

    def register(self, provider: Provider) -> None:
        self._providers.append(provider)

    def get(self, name: str) -> Provider | None:
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def ordered(self, kind: ProviderKind | None = None,
                cost_filter: set[ProviderCost] | None = None,
                configured_only: bool = True) -> list[Provider]:
        """Return providers sorted by priority (ascending = cheapest/most-likely first)."""
        result = self._providers
        if kind:
            result = [p for p in result if p.kind == kind]
        if cost_filter:
            result = [p for p in result if p.cost in cost_filter]
        if configured_only:
            result = [p for p in result if p.is_configured()]
        return sorted(result, key=lambda p: p.priority)

    def call(self, name: str, **kwargs: Any) -> dict:
        """Call a provider by name, checking quota first."""
        from .quota import QUOTA
        p = self.get(name)
        if p is None:
            return {"ok": False, "error": f"unknown provider: {name}"}
        if not p.is_configured():
            return {"ok": False, "error": "not configured", "skip": True}
        # Check quota for free-tier providers
        if p.cost == ProviderCost.FREE_TIER:
            key = QUOTA.next_key(p.name)
            if key is None:
                return {"ok": False, "error": "quota exhausted", "skip": True, "quota": True}
            kwargs.setdefault("_api_key", key)
        result = p.call(**kwargs)
        # Record the call and detect quota/rate signals
        if p.cost == ProviderCost.FREE_TIER:
            key = kwargs.get("_api_key", "default")
            status = result.get("status", 200) if result.get("ok") else result.get("status", 0) or 0
            QUOTA.record_call(p.name, key, status, result)
        return result

    def all_names(self, kind: ProviderKind | None = None) -> list[str]:
        return [p.name for p in self.ordered(kind=kind, configured_only=False)]


# ---------------------------------------------------------------------------
# Global registry singleton
# ---------------------------------------------------------------------------

REGISTRY = ProviderRegistry()


def _noop(**_: Any) -> dict:
    return {"ok": False, "error": "not implemented", "skip": True}


# ---------------------------------------------------------------------------
# Built-in FREE (keyless) providers — registered at import time
# ---------------------------------------------------------------------------

# MX + DoH — always available, highest priority
REGISTRY.register(Provider(
    name="dns_mx",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE,
    priority=10,
    description="MX record lookup with DoH fallback",
    call_fn=_noop,  # called directly via dns_resolve.mx() — not via registry
))

# Gravatar existence check
REGISTRY.register(Provider(
    name="gravatar",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE,
    priority=20,
    description="Gravatar avatar-404 existence check (SHA256)",
    call_fn=_noop,
))

# SMTP probe (scored signal only)
REGISTRY.register(Provider(
    name="smtp_probe",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE,
    priority=30,
    description="SMTP RCPT probe (probabilistic — scored signal only)",
    call_fn=_noop,
))

# crt.sh CT log search (keyless)
REGISTRY.register(Provider(
    name="crt_sh",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE,
    priority=15,
    description="Certificate Transparency log search via crt.sh",
    call_fn=_noop,
))

# PGP keyservers
REGISTRY.register(Provider(
    name="pgp_keyservers",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE,
    priority=20,
    description="PGP public keyserver search (keys.openpgp.org etc.)",
    call_fn=_noop,
))

# GitHub commit email extraction
REGISTRY.register(Provider(
    name="github_commits",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE,
    priority=25,
    description="GitHub commit-author emails (keyless; token lifts rate limit)",
    env_keys=[],
    call_fn=_noop,
))

# ---------------------------------------------------------------------------
# FREE-TIER providers (need env key, have monthly cap)
# ---------------------------------------------------------------------------

REGISTRY.register(Provider(
    name="hunter",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=100,
    description="Hunter.io email finder (50/mo free)",
    env_keys=["HUNTER_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="hunter_verify",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE_TIER,
    priority=100,
    description="Hunter.io email verifier (50/mo free)",
    env_keys=["HUNTER_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="tomba",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=110,
    description="Tomba email finder (50/mo free)",
    env_keys=["TOMBA_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="reoon",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE_TIER,
    priority=120,
    description="Reoon email verifier (pay-as-you-go; de-prioritized)",
    env_keys=["REOON_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="verifalia",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE_TIER,
    priority=105,
    description="Verifalia email verifier (~750/mo free)",
    env_keys=["VERIFALIA_USERNAME", "VERIFALIA_PASSWORD"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="abstract",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE_TIER,
    priority=115,
    description="Abstract API email validation (100/mo free)",
    env_keys=["ABSTRACT_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="mailboxlayer",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE_TIER,
    priority=118,
    description="MailboxLayer SMTP+disposable verifier (~100/mo free)",
    env_keys=["MAILBOXLAYER_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="skrapp",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=125,
    description="Skrapp email finder (100/mo free)",
    env_keys=["SKRAPP_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="snov",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=130,
    description="Snov.io email finder (50/mo free; API support-gated)",
    env_keys=["SNOV_USER_ID", "SNOV_SECRET"],
    call_fn=_noop,
))

# Keyless verifiers — NO key, NO signup, always run (free). Low priority numbers so they're tried first.
REGISTRY.register(Provider(
    name="rapid",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE_TIER,
    priority=10,
    description="Rapid Email Verifier — free, open-source, no key (syntax/MX/disposable)",
    env_keys=[],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="disify",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE_TIER,
    priority=11,
    description="Disify — free, no key (format/DNS/disposable)",
    env_keys=[],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="myemailverifier",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE_TIER,
    priority=112,
    description="MyEmailVerifier — 100 free/DAY, no card",
    env_keys=["MYEMAILVERIFIER_API_KEY"],
    call_fn=_noop,
))

# Finders (name+domain → email)
REGISTRY.register(Provider(
    name="prospeo",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=108,
    description="Prospeo email/LinkedIn finder (75-100 free, no card)",
    env_keys=["PROSPEO_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="getprospect",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=122,
    description="GetProspect email finder (50/mo free)",
    env_keys=["GETPROSPECT_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="minelead",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=128,
    description="Minelead email finder (25/mo free)",
    env_keys=["MINELEAD_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="emailverify_io",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=124,
    description="EmailVerify.io finder (10 finds + 100 verifies/mo free)",
    env_keys=["EMAILVERIFY_IO_API_KEY"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="generect",
    kind=ProviderKind.FIND,
    cost=ProviderCost.FREE_TIER,
    priority=126,
    description="Generect email finder (50 free, no card)",
    env_keys=["GENERECT_API_KEY"],
    call_fn=_noop,
))

# ---------------------------------------------------------------------------
# OSS / self-hosted (optional — degrade cleanly if absent)
# ---------------------------------------------------------------------------

REGISTRY.register(Provider(
    name="reacher",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE,
    priority=50,
    description="Reacher self-hosted SMTP verifier (Docker HTTP service)",
    env_keys=["REACHER_BASE_URL"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="firecrawl",
    kind=ProviderKind.HARVEST,
    cost=ProviderCost.FREE,
    priority=40,
    description="Firecrawl self-hosted LLM-ready scraper (Docker HTTP service)",
    env_keys=["FIRECRAWL_URL"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="theharvester",
    kind=ProviderKind.OSINT,
    cost=ProviderCost.FREE,
    priority=60,
    description="theHarvester passive OSINT aggregator (40+ sources)",
    optional_deps=["theHarvester"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="holehe",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE,
    priority=45,
    description="Holehe account-existence enumeration (120+ sites)",
    optional_deps=["holehe"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="ghunt",
    kind=ProviderKind.VERIFY,
    cost=ProviderCost.FREE,
    priority=48,
    description="GHunt reverse Gmail→Google account (name, services)",
    optional_deps=["ghunt"],
    call_fn=_noop,
))

REGISTRY.register(Provider(
    name="maigret",
    kind=ProviderKind.OSINT,
    cost=ProviderCost.FREE,
    priority=70,
    description="Maigret username→3000+ profiles OSINT",
    optional_deps=["maigret"],
    call_fn=_noop,
))


def update_call_fn(name: str, fn: Callable) -> None:
    """Replace the call_fn of a registered provider (called during server init)."""
    p = REGISTRY.get(name)
    if p:
        p.call_fn = fn
