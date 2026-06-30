"""SkillContentScanner — fetch a hub skill's real content and statically analyze it.

The pre-install skill scan used to be "theater": with no fetchable coordinate it
analyzed nothing, so every community skill scored an identical metadata-only
verdict (signature-unsigned + no-url) regardless of what the skill actually does.
This scanner mirrors the MCP/package model — fetch the published bytes and run a
real static analysis — so the verdict DISCRIMINATES (a benign skill scores
differently from a dangerous one).

Security:
- The fetch reuses ``tools.skills_hub`` (the SAME bounded, SSRF-guarded fetch the
  installer uses); files are READ, never executed.
- Runs off the daemon loop via ``asyncio.to_thread`` (bounded network + CPU).
- Fail-loud: a coverable skill we cannot fetch/inspect yields a
  ``content:unanalyzable`` HIGH (capped to WARN/FAIL by ``_compose_score``),
  never a silent PASS.
"""

from __future__ import annotations

import asyncio
import logging

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Risk, Severity

logger = logging.getLogger("hermes.security_center.skill_content")

_SEV_MAP = {
    "CRITICAL": Severity.CRITICAL,
    "HIGH": Severity.HIGH,
    "MEDIUM": Severity.MEDIUM,
}

# Bounds so a hostile/huge bundle cannot exhaust the scan thread.
_MAX_FILES = 80
_MAX_TEXT_BYTES = 2_000_000


class SkillContentScanner:
    """IScanner: static content analysis of a hub skill's SKILL.md + bundled files."""

    name = "skill_content"

    async def scan(self, target: InstallTarget) -> list[Risk]:
        if target.kind != "skill":
            return []
        ident = (target.identifier or "").strip()
        if not ident:
            return []
        try:
            text = await asyncio.to_thread(self._fetch_skill_text, ident)
        except Exception as exc:  # noqa: BLE001 — fetch is best-effort, fail loud not silent
            logger.warning("hermes.security.skill_content_fetch_error %s: %s", ident, exc)
            return [self._unanalyzable(ident, str(exc) or type(exc).__name__)]
        if not text:
            return [self._unanalyzable(ident, "no se encontró/pudo descargar el contenido")]
        try:
            return self._analyze(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.security.skill_content_analyze_error %s: %s", ident, exc)
            return [self._unanalyzable(ident, f"error analizando contenido: {exc}")]

    @staticmethod
    def _fetch_skill_text(identifier: str) -> str:
        # Deferred import: tools.skills_hub is the baked Nous package (absent on the
        # host/CI). If it's missing the except in scan() turns it into unanalyzable.
        from tools.skills_hub import create_source_router  # noqa: PLC0415

        bundle = None
        for src in create_source_router():
            try:
                bundle = src.fetch(identifier)
            except Exception:  # noqa: BLE001 — try the next source
                bundle = None
            if bundle:
                break
        files = getattr(bundle, "files", None) if bundle is not None else None
        if not files:
            return ""

        parts: list[str] = []
        total = 0
        for _name, content in list(files.items())[:_MAX_FILES]:
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            if not isinstance(content, str):
                continue
            parts.append(content)
            total += len(content)
            if total >= _MAX_TEXT_BYTES:
                break
        return "\n".join(parts)

    @staticmethod
    def _analyze(text: str) -> list[Risk]:
        from hermes.agents_os.domain.skill_content_scan import (  # noqa: PLC0415
            scan_skill_markdown,
        )

        risks: list[Risk] = []
        for f in scan_skill_markdown(text):
            sev_key = getattr(f.severity, "value", str(f.severity))
            risks.append(
                Risk(
                    category="content",
                    severity=_SEV_MAP.get(sev_key, Severity.MEDIUM),
                    message=(f.message or "patrón sospechoso en el contenido del skill"),
                    evidence_ref=f"content:skill:{getattr(f, 'pattern', '?')}",
                )
            )
        return risks

    @staticmethod
    def _unanalyzable(identifier: str, detail: str) -> Risk:
        # Absence of analysis must never read as PASS. _compose_score caps a content
        # HIGH to ≤45 → WARN/FAIL (owner review).
        return Risk(
            category="content",
            severity=Severity.HIGH,
            message=(
                f"No se pudo inspeccionar el contenido del skill «{identifier}» "
                f"({detail}) — requiere revisión del dueño."
            ),
            evidence_ref=f"content:unanalyzable:skill:{identifier}",
        )
