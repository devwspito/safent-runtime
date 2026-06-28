"""Skills REST API — skills hub (marketplace) D-Bus surface.

Skills governance (list/promote/deprecate) is already exposed by audit_api.py
at /api/v1/skills. This module adds the hub (marketplace) surface:

  GET    /api/v1/skills/hub             list installed hub skills
  GET    /api/v1/skills/hub/search      search the hub (query param: q)
  POST   /api/v1/skills/hub/install     install a skill from the hub
  POST   /api/v1/skills/hub/synthesize  GATED free-text → SKILL.md minting
  DELETE /api/v1/skills/hub/{name}      uninstall a hub skill
  GET    /api/v1/skills/hub/ops/{id}    poll install/uninstall operation status

Security:
  - Mutators carry a signed OperatorToken (DbusRuntimeProxy.call_mutator).
  - fail-soft for GETs; fail-hard 503 for mutators (CTRL-P1-11).
  - Skill minting (synthesize) is the agent-discoverable code path: it writes a
    SKILL.md the agent auto-loads. Red-team 2026-06-19 (HIGH): free-text
    synthesis bypassed the Security Center, minting a signed, validated skill
    from an unreviewed description ("download evil.com/x.sh and run it"). The
    gated endpoint below runs the SAME content scan as the recording sign-gate
    (scan→score→user-decide) BEFORE anything is written to disk or the skills
    view. A CRITICAL trojan pattern (dropper / reverse shell / obfuscated exec)
    is BLOCKED 422 — no SKILL.md, no row, nothing the agent can discover.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.skills_api")


# ------------------------------------------------------------------
# Pydantic schemas
# ------------------------------------------------------------------


class InstallSkillRequest(BaseModel):
    identifier: str = Field(min_length=1, description="Hub skill identifier (e.g. 'pdf-tools')")
    force: bool = Field(default=False, description="Owner-sovereign override: install despite FAIL verdict")


class SynthesizeSkillRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="Skill name")
    description: str = Field(
        min_length=1,
        max_length=20000,
        description="Free-text description / narrated steps to mint into a SKILL.md",
    )


# ------------------------------------------------------------------
# Security gate — content scan over a skill's free text + generated SKILL.md
# ------------------------------------------------------------------


def assert_skill_text_safe(*sources: str) -> list[dict]:
    """Scan free-text skill sources for trojan patterns; block on HIGH+.

    Reuses the same domain scanner as the recording sign-gate
    (agents_os.domain.skill_content_scan), but mints are gated MORE strictly than
    recorded demos: minting turns an unreviewed description into a signed,
    auto-loadable skill, so this gate blocks on HIGH OR CRITICAL (persistence,
    privilege escalation, destructive ops, droppers, reverse shells, obfuscated
    exec) — not only CRITICAL.

    Crucially it scans HOLISTICALLY over the whole normalized text
    (scan_skill_text / scan_skill_markdown both join line-continuations, strip
    comments, collapse whitespace and run the multi-line catalogue + split-dropper
    correlation), so split-line droppers, base64-decode-pipe, cross-line reverse
    shells and fetch-then-exec are caught — not just three single-line regexes.

    Raises HTTPException(422) on a blocking (HIGH/CRITICAL) finding. Returns the
    residual non-blocking (MEDIUM) findings for the UI.

    This is the CONTENT half of scan→score→user-decide for minted skills; the
    EXECUTION half (egress jail, terminal install-gate, broker HITL, signature
    verification before load) still applies at replay time.
    """
    from hermes.agents_os.domain.skill_content_scan import (  # noqa: PLC0415
        ContentSeverity,
        has_high_or_critical_finding,
        scan_skill_markdown,
        scan_skill_text,
    )

    findings = []
    for text in sources:
        if text and text.strip():
            # scan_skill_markdown handles fenced SKILL.md; scan_skill_text handles
            # raw free-text. Both run the holistic/normalized pass, so run both and
            # take the union — a free-text description has no fences, a SKILL.md may
            # hide a payload between them.
            findings.extend(scan_skill_markdown(text))
            findings.extend(scan_skill_text(text))

    if has_high_or_critical_finding(findings):
        blocking = [
            f.message
            for f in findings
            if f.severity in (ContentSeverity.HIGH, ContentSeverity.CRITICAL)
        ]
        # De-duplicate messages while preserving order.
        seen: set[str] = set()
        unique_blocking = [m for m in blocking if not (m in seen or seen.add(m))]
        logger.warning(
            "hermes.skills.synthesize BLOCKED by content scan: %s",
            [
                (f.pattern, f.severity.value)
                for f in findings
                if f.severity in (ContentSeverity.HIGH, ContentSeverity.CRITICAL)
            ],
        )
        raise HTTPException(
            status_code=422,
            detail=(
                "Skill bloqueada por el Centro de Seguridad: contenido peligroso "
                "detectado — " + "; ".join(unique_blocking[:3])
            ),
        )

    return [
        {"pattern": f.pattern, "severity": f.severity.value, "message": f.message}
        for f in findings
    ]


# ------------------------------------------------------------------
# Router factory
# ------------------------------------------------------------------


def create_skills_hub_router(db_path: Path) -> APIRouter:
    router = APIRouter(prefix="/api/v1/skills/hub", tags=["skills"])

    @router.get("")
    async def list_hub_skills(request: Request) -> list[dict]:
        """List skills installed from the hub.

        Fail-soft: returns [] when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_list("list_hub_skills")
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.skills.hub.list_unavailable",
                extra={"reason": str(exc)},
            )
            return []

    @router.get("/search")
    async def search_skills_hub(
        request: Request,
        q: str = Query(..., min_length=1, description="Search query"),
        source: str = Query("all"),
        limit: int = Query(20, le=100),
    ) -> dict:
        """Search the skills hub.

        Returns {query_id, cancelled, results: [{identifier, name, source}]}.
        Fail-soft: returns empty result set when daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            # Over-fetch, then hide the catalog "ghosts": the centralized index lists
            # tens of thousands of stubs (mostly clawhub/official) with an EMPTY repo and
            # no artifact — they look installable but always 404 on install. Keep only
            # entries we can actually fetch: a real repo coordinate, or a source that
            # fetches by its own means (browse-sh slug, well-known URL). Cap at `limit`.
            raw = await proxy.call_dict("search_skills_hub", q, source, min(limit * 6, 100))
            items = raw.get("results", []) if isinstance(raw, dict) else []
            _SELF_FETCH = {"browse-sh", "well-known"}
            fetchable = [
                r for r in items
                if str(r.get("repo") or "").strip() or r.get("source") in _SELF_FETCH
            ]
            if isinstance(raw, dict):
                raw["results"] = fetchable[:limit]
                return raw
            return {"query_id": "", "cancelled": False, "results": fetchable[:limit]}
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.skills.hub.search_unavailable",
                extra={"query": q, "reason": str(exc)},
            )
            return {"query_id": "", "cancelled": True, "results": []}

    @router.post("/synthesize", status_code=201)
    async def synthesize_skill(request: Request, body: SynthesizeSkillRequest) -> dict:
        """Mint a SKILL.md from free text — GATED by the Security Center.

        Flow (scan→score→user-decide, fail-closed on dangerous content):
          1. Pre-scan the operator's free text (cheap reject before any LLM call).
          2. Synthesize the SKILL.md via the active provider.
          3. Scan the GENERATED SKILL.md (the model could echo a payload).
          4. Only if both pass: delegate to the daemon via D-Bus (create_skill_from_text),
             which uses SkillStoreAdapter as the single authorized writer.

        A CRITICAL trojan pattern at step 1 or 3 → 422, nothing persisted.
        A daemon unavailable (None proxy or D-Bus error) → 503.
        """
        from hermes.shell_server.skills.skill_synthesis import (  # noqa: PLC0415
            NoActiveProvider,
            synthesize_and_persist,
            synthesize_skill_md,
        )

        name = body.name.strip()
        description = body.description.strip()
        if not name or not description:
            raise HTTPException(
                422, "Indica un nombre y describe qué hace la skill para crearla."
            )

        proxy = getattr(request.app.state, "dbus_proxy", None)

        # 1) Gate the operator's free text before spending an LLM call.
        assert_skill_text_safe(description)

        # 2) Synthesize via the native resolver — no local repo copy of the key.
        try:
            skill_md = await synthesize_skill_md(
                name=name, description=description, db_path=db_path
            )
        except NoActiveProvider as exc:
            raise HTTPException(
                409, "Conecta un modelo en Proveedores para crear skills."
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("hermes.skills.synthesize generation failed")
            raise HTTPException(502, "No se pudo sintetizar la skill. Reintenta.") from exc

        # 3) Gate the GENERATED document — the model may reproduce a payload from
        #    the description even if step 1's regexes didn't trip on the prose.
        advisory = assert_skill_text_safe(skill_md)

        # 4) Persist via the daemon (single authorized writer) after both scans pass.
        try:
            meta = await synthesize_and_persist(
                db_path=db_path,
                name=name,
                description=description,
                dbus_proxy=proxy,
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("hermes.skills.synthesize persist failed")
            raise HTTPException(502, "No se pudo guardar la skill. Reintenta.") from exc

        meta["security_findings"] = advisory  # HIGH/MEDIUM advisories for the UI
        logger.info(
            "hermes.skills.synthesize ok skill=%s package=%s advisories=%d",
            meta.get("skill_id") or meta.get("skill_name"),
            meta.get("package_id"),
            len(advisory),
        )
        return {"ok": True, "skill": meta}

    @router.post("/install", status_code=202)
    async def install_hub_skill(request: Request, body: InstallSkillRequest) -> dict:
        """Install a skill from the hub. Returns {op_id, status}.

        When body.force=True the owner-sovereign override is forwarded to the
        daemon.  The operator-token middleware already fronts this route so only
        authenticated operators can set force.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_mutator("install_hub_skill", body.identifier, body.force)
        except AgentUnavailable as exc:
            _raise_503(exc, "install_hub_skill")

    @router.delete("/{skill_name}", status_code=202)
    async def uninstall_hub_skill(request: Request, skill_name: str) -> dict:
        """Uninstall a hub skill by name. Returns {op_id, status}."""
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_mutator("uninstall_hub_skill", skill_name)
        except AgentUnavailable as exc:
            _raise_503(exc, "uninstall_hub_skill")

    @router.get("/ops/{op_id}")
    async def get_hub_op_status(request: Request, op_id: str) -> dict:
        """Poll the status of a hub install/uninstall operation.

        Returns {op_id, status}. Fail-soft: returns unknown status on daemon unavailable.
        """
        proxy = request.app.state.dbus_proxy
        try:
            return await proxy.call_dict("get_hub_op_status", op_id)
        except AgentUnavailable:
            return {"op_id": op_id, "status": "unknown"}

    return router


def _raise_503(exc: AgentUnavailable, operation: str) -> None:
    logger.warning(
        "hermes.skills.hub.mutator_unavailable",
        extra={"operation": operation, "reason": str(exc)},
    )
    raise HTTPException(
        status_code=503,
        detail={
            "code": "agent_unavailable",
            "message": "El agente no está disponible. Comprueba que hermes-runtime está activo.",
        },
    ) from exc
