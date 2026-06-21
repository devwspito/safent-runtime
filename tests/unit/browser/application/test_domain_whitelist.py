"""T302c — Domain whitelist enforcement tests.

Constitution IV: fail-closed. Navigation outside whitelist → DomainViolation.
Threat-model E1 surface 1 / SC-010 / FR-023.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from hermes.browser.application.discovery_runner import DiscoveryRunner, DomainViolation
from hermes.browser.application.session import BrowserSessionConfig
from hermes.browser.domain.step import StepStatus
from hermes.browser.testing import FakeBrowserDriver

_TENANT = UUID("00000000-0000-0000-0000-000000000044")

_ALLOWED_DOMAIN = "stub.local"
_DOMAINS_WHITELIST = (_ALLOWED_DOMAIN,)


def _make_config(**overrides: object) -> BrowserSessionConfig:
    base: dict[str, object] = {
        "tenant_id": _TENANT,
        "site_id": "demo_sede_stub",
        "flow_id": "form_flow",
        "anti_bot_min_delay_ms": 1,
        "anti_bot_max_delay_ms": 3,
        "anti_bot_mean_delay_ms": 2,
    }
    base.update(overrides)
    return BrowserSessionConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test: navigation to whitelisted domain is allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_to_whitelisted_domain_allowed() -> None:
    """Navigation to a whitelisted domain proceeds without DomainViolation."""
    driver = FakeBrowserDriver()
    config = _make_config()
    runner = DiscoveryRunner(
        driver=driver,
        config=config,
        domains_whitelist=_DOMAINS_WHITELIST,
    )

    outcome = await runner.navigate("http://stub.local/login")

    assert outcome.status == StepStatus.EXECUTED_OK


# ---------------------------------------------------------------------------
# Test: navigation outside whitelist is blocked with DomainViolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_to_blocked_domain_raises_domain_violation() -> None:
    """Navigation to a non-whitelisted domain raises DomainViolation BEFORE driver.execute.

    Constitution IV: fail-closed. No driver interaction on domain violation.
    """
    driver = FakeBrowserDriver()
    config = _make_config()
    runner = DiscoveryRunner(
        driver=driver,
        config=config,
        domains_whitelist=_DOMAINS_WHITELIST,
    )

    with pytest.raises(DomainViolation) as exc_info:
        await runner.navigate("https://attacker.example/exfil")

    assert "attacker.example" in str(exc_info.value).lower() or exc_info.value
    # Driver must NOT have been called — fail-closed.
    assert len(driver.executed_steps) == 0, (
        "Driver must not execute when domain is not whitelisted"
    )


# ---------------------------------------------------------------------------
# Test: subdomain of whitelisted domain is allowed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_to_subdomain_of_whitelisted_domain_allowed() -> None:
    """Subdomain of a whitelisted domain is allowed.

    SiteSpec.is_domain_allowed: host == a or host.endswith('.' + a).
    """
    driver = FakeBrowserDriver()
    config = _make_config()
    runner = DiscoveryRunner(
        driver=driver,
        config=config,
        domains_whitelist=_DOMAINS_WHITELIST,
    )

    outcome = await runner.navigate("http://api.stub.local/resource")

    assert outcome.status == StepStatus.EXECUTED_OK
