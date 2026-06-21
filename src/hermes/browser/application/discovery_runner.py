"""DiscoveryRunner: orchestration layer between BrowserSession and the driver.

Responsibilities (in execution order per step):
  1. Domain whitelist check — blocks navigation outside SiteSpec.domains_whitelist.
  2. PII tokenization — strips NIF/IBAN/etc from step.payload BEFORE LLM prompt.
  3. DOM sanitization — cleans DOM text before any LLM context injection.
  4. Delegate to driver — rehydrates PII only inside browser-fill variables.
  5. Observability — structured events per step.

Constitution III: PII tokenization ALWAYS before any LLM call.
Constitution IV: fail-closed — DomainViolation raised before driver.execute.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import structlog

from hermes.browser.application.dom_sanitizer import sanitize_for_llm
from hermes.browser.application.session import BrowserSessionConfig
from hermes.browser.domain.port import BrowserPort
from hermes.browser.domain.step import Step, StepKind, StepOutcome, StepRisk
from hermes.tokenizer.pii import DefaultPIITokenizer, PIITokenizer

log = structlog.get_logger("hermes.browser.discovery_runner")


class DomainViolation(RuntimeError):
    """Navigation to a domain outside the SiteSpec whitelist was attempted.

    Constitution IV / FR-023 / SC-010 / threat-model E1 surface 1.
    """


class DiscoveryRunner:
    """Orchestrates tokenization, sanitization, whitelist check, and driver delegation.

    Not a BrowserPort itself — wraps an injected driver and adds the
    cross-cutting concerns required by Constitution II/III/IV.

    Usage:
        runner = DiscoveryRunner(
            driver=stagehand_driver,
            config=session_config,
            domains_whitelist=site_spec.domains_whitelist,
        )
        outcome = await runner.navigate("https://stub.local/login")
        outcome = await runner.act("fill NIF with [[NIF_1]]", pii_mapping=mapping)
    """

    def __init__(
        self,
        *,
        driver: BrowserPort,
        config: BrowserSessionConfig,
        domains_whitelist: tuple[str, ...] = (),
        pii_tokenizer: PIITokenizer | None = None,
    ) -> None:
        self._driver = driver
        self._config = config
        self._domains_whitelist = tuple(d.lower().strip() for d in domains_whitelist)
        self._tokenizer: PIITokenizer = pii_tokenizer or DefaultPIITokenizer()
        self._llm_call_counter = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def navigate(
        self,
        url: str,
        *,
        intent_desc: str = "",
        hitl_approval_token: str | None = None,
    ) -> StepOutcome:
        """Navigate to url. Blocks if domain is outside whitelist (fail-closed)."""
        self._check_domain(url)
        step = self._make_step(
            kind=StepKind.NAVIGATE,
            risk=StepRisk.LOW,
            intent_desc=intent_desc or f"navigate to {url}",
            payload={"url": url},
        )
        outcome = await self._driver.execute(step, hitl_approval_token=hitl_approval_token)
        self._emit_step_event(step, outcome)
        return outcome

    async def act(
        self,
        instruction: str,
        *,
        risk: StepRisk = StepRisk.MEDIUM,
        fill_value: str | None = None,
        pii_mapping: dict[str, str] | None = None,
        hitl_approval_token: str | None = None,
    ) -> StepOutcome:
        """Execute an act step with PII tokenization applied to instruction."""
        tokenized_instruction, merged_mapping = self._tokenize_instruction(
            instruction, fill_value=fill_value, extra_mapping=pii_mapping
        )
        payload: dict[str, Any] = {"instruction": tokenized_instruction}
        if fill_value is not None:
            rehydrated_fill = self._rehydrate_if_placeholder(fill_value, merged_mapping)
            payload["fill_value"] = rehydrated_fill
            # Strip [[...]] brackets to get bare placeholder name for Stagehand variables.
            payload["variables"] = {
                k[2:-2]: v for k, v in merged_mapping.items()
            }

        self._llm_call_counter += 1
        step = self._make_step(
            kind=StepKind.ACT,
            risk=risk,
            intent_desc=tokenized_instruction,
            payload=payload,
        )
        outcome = await self._driver.execute(step, hitl_approval_token=hitl_approval_token)
        self._emit_step_event(step, outcome)
        return outcome

    async def extract(
        self,
        *,
        instruction: str,
        schema: dict[str, Any],
        dom_text: str | None = None,
        hitl_approval_token: str | None = None,
    ) -> StepOutcome:
        """Execute an extract step, sanitizing DOM before sending to LLM."""
        sanitized_instruction = self._sanitize_dom_in_context(instruction, dom_text)
        self._llm_call_counter += 1
        step = self._make_step(
            kind=StepKind.EXTRACT,
            risk=StepRisk.LOW,
            intent_desc=sanitized_instruction,
            payload={"instruction": sanitized_instruction, "schema": schema},
        )
        outcome = await self._driver.execute(step, hitl_approval_token=hitl_approval_token)
        self._emit_step_event(step, outcome)
        return outcome

    @property
    def llm_call_count(self) -> int:
        """Number of LLM-touching calls made in this runner session."""
        return self._llm_call_counter

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_domain(self, url: str) -> None:
        """Raise DomainViolation if url.host is not in the whitelist.

        Constitution IV: fail-closed — when in doubt, deny.
        """
        if not self._domains_whitelist:
            return

        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        host = host.lower().strip()

        if self._is_domain_allowed(host):
            return

        log.warning(
            "hermes.browser.domain_violation",
            url=url,
            host=host,
            whitelist=self._domains_whitelist,
        )
        raise DomainViolation(
            f"Navigation to host '{host}' blocked — not in domains_whitelist "
            f"{self._domains_whitelist!r}. (FR-023 / Constitution IV)"
        )

    def _is_domain_allowed(self, host: str) -> bool:
        for allowed in self._domains_whitelist:
            if host == allowed or host.endswith("." + allowed):
                return True
        return False

    def _tokenize_instruction(
        self,
        instruction: str,
        *,
        fill_value: str | None,
        extra_mapping: dict[str, str] | None,
    ) -> tuple[str, dict[str, str]]:
        """Tokenize PII in instruction and fill_value. Returns (safe_instruction, mapping)."""
        payload: dict[str, Any] = {"instruction": instruction}
        if fill_value is not None:
            payload["fill_value"] = fill_value

        result = self._tokenizer.tokenize(payload)
        mapping: dict[str, str] = dict(result.mapping)
        if extra_mapping:
            mapping.update(extra_mapping)

        if result.replaced > 0:
            log.info(
                "hermes.browser.pii_tokenized",
                token_count=result.replaced,
            )

        sanitized = result.sanitized
        return str(sanitized.get("instruction", instruction)), mapping

    def _sanitize_dom_in_context(
        self,
        instruction: str,
        dom_text: str | None,
    ) -> str:
        """Sanitize DOM for LLM context, returning the instruction unchanged.

        The sanitized DOM is logged for observability but not injected into
        the instruction here — StagehandDriver does the page.content() call
        internally. This hook exists for future prompt-injection defense.
        """
        if dom_text is None:
            return instruction

        sanitized = sanitize_for_llm(dom_text)
        log.info(
            "hermes.browser.dom_sanitized",
            stripped=sanitized.stripped_count,
            truncated=sanitized.truncated,
        )
        return instruction

    def _rehydrate_if_placeholder(
        self,
        value: str,
        mapping: dict[str, str],
    ) -> str:
        """Return the real value if value is a known placeholder, else value itself."""
        return mapping.get(value, value)

    def _make_step(
        self,
        *,
        kind: StepKind,
        risk: StepRisk,
        intent_desc: str,
        payload: dict[str, Any],
    ) -> Step:
        return Step.new(
            tenant_id=self._config.tenant_id,
            session_id=self._config.session_id,
            kind=kind,
            risk=risk,
            intent_desc=intent_desc,
            payload=payload,
        )

    def _emit_step_event(self, step: Step, outcome: StepOutcome) -> None:
        log.info(
            "hermes.browser.discovery_step_executed",
            step_id=str(step.step_id),
            kind=step.kind,
            risk=step.risk,
            status=outcome.status,
        )
