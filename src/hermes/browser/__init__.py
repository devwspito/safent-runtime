"""hermes.browser — capa de browser automation universal.

Diseno 4-tier basado en research SOTA (mayo 2026):

    Tier 1 — Stagehand (MIT, Python): driver default para flujos estables.
             Action caching + AI fallback + selectores firmados HMAC.
    Tier 2 — browser-use (MIT, Python): discovery cuando el flujo no esta
             pre-mapeado. Mismo Chromium via Playwright `browser.bind()`.
    Tier 3 — Anthropic computer-use: vision-only escape hatch para canvas/
             SVG/Flash. Budget cap por sesion; si excede -> HITL.
    Tier 4 — Playwright CLI replay: tras un run exitoso, compila a script
             determinista firmado. Subsequent runs sin LLM.

Capa diferencial de Hermes:
    - SelectorRegistry firmado HMAC SHA-256 (anti-tampering).
    - StepRecorder con screenshot + DOM snapshot diff.
    - HITL gate en steps HIGH (TOTP del titular).
    - Anti-bot: delays lognormal, UA realista, NO captcha bypass.
    - Cert PSC inject via PKCS#11 NSS en tmpfs (zeroize en container exit).
    - Egress whitelist via nftables sidecar (gVisor sandbox).

Importes publicos minimos:
    from hermes.browser import (
        BrowserPort,
        BrowserSession,
        BrowserSessionConfig,
        Step,
        StepRisk,
        StepOutcome,
        Selector,
        SelectorRegistry,
        SignedSelectorRegistry,
        Screenshot,
        DomSnapshot,
    )
"""

from hermes.browser.application.session import BrowserSession, BrowserSessionConfig
from hermes.browser.domain.port import BrowserPort
from hermes.browser.domain.selector import Selector, SelectorRegistry
from hermes.browser.domain.snapshot import DomSnapshot, Screenshot, ScreenshotDiff
from hermes.browser.domain.step import (
    Step,
    StepKind,
    StepOutcome,
    StepRisk,
    StepStatus,
)
from hermes.browser.infrastructure.signed_selector_registry import (
    SelectorTamperedError,
    SignedSelectorRegistry,
)

__all__ = [
    "BrowserPort",
    "BrowserSession",
    "BrowserSessionConfig",
    "DomSnapshot",
    "Screenshot",
    "ScreenshotDiff",
    "Selector",
    "SelectorRegistry",
    "SelectorTamperedError",
    "SignedSelectorRegistry",
    "Step",
    "StepKind",
    "StepOutcome",
    "StepRisk",
    "StepStatus",
]
