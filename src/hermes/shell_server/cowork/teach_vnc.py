"""teach_vnc — record a demonstration in the JAILED browser shown via noVNC.

The jailed Chromium now runs HEADFUL (Xvfb + x11vnc); the web UI shows it SHARP via
noVNC and the user drives it directly (real browser, address bar and all). To turn a
demonstration into a skill we OBSERVE that browser over CDP: inject a small recorder
(clicks/inputs/navigations → robust selector) and feed the steps into the existing
TrainingSessionOrchestrator → SkillCompiler → SKILL.md.

Fully UI-driven (POST /api/v1/teach/start + /save); no terminal, no extension. The
observer runs server-side against the shared jailed browser CDP (10.200.0.2:9333).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import sqlite3
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request

from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger("hermes.shell_server.cowork.teach_vnc")

OBSERVER_JS: str = r"""
(() => {
  if (window.__lumenObserverInstalled) return;
  window.__lumenObserverInstalled = true;
  function esc(s){ try { return CSS.escape(s); } catch(_) { return s; } }
  function sel(el){
    if(!el||el.nodeType!==1) return null;
    if(el.id) return '#'+esc(el.id);
    const parts=[]; let n=el;
    while(n&&n.nodeType===1&&parts.length<5){
      let p=n.tagName.toLowerCase();
      const nm=n.getAttribute&&n.getAttribute('name');
      if(nm){ p+='[name="'+nm+'"]'; parts.unshift(p); break; }
      const par=n.parentElement;
      if(par){ const sib=Array.from(par.children).filter(c=>c.tagName===n.tagName);
        if(sib.length>1) p+=':nth-of-type('+(sib.indexOf(n)+1)+')'; }
      parts.unshift(p); n=n.parentElement;
    }
    return parts.join(' > ');
  }
  function desc(el){
    if(!el||el.nodeType!==1) return {};
    return { tag: el.tagName ? el.tagName.toLowerCase() : null,
      role: (el.getAttribute && el.getAttribute('role')) || null,
      text: ((el.innerText||el.value||el.getAttribute&&el.getAttribute('aria-label')||'')+'').trim().slice(0,80),
      id: el.id||null, name: (el.getAttribute && el.getAttribute('name'))||null, selector: sel(el) };
  }
  function rec(o){ try { window.__lumenRec(JSON.stringify(o)); } catch(_){} }
  document.addEventListener('click', e => rec({type:'click', element:desc(e.target)}), true);
  document.addEventListener('change', e => rec({type:'input',
    value:((e.target&&e.target.value)||'').slice(0,500), element:desc(e.target)}), true);
})();
"""


def _event_to_payload(ev: dict) -> dict | None:
    kind = ev.get("type")
    if kind == "navigate":
        url = str(ev.get("url", "")).strip()
        return {"kind": "navigate", "url": url} if url else None
    el = ev.get("element") or {}
    if kind == "click":
        return {"kind": "act", "action": "click", "element": el}
    if kind == "input":
        return {"kind": "act", "action": "key", "text": str(ev.get("value", ""))[:500],
                "element": el}
    return None


class _Recorder:
    """Server-side CDP observer of the shared jailed browser for one session."""

    def __init__(self, cdp_url, orchestrator, session_id):
        self._cdp_url = cdp_url
        self._orch = orchestrator
        self._sid = session_id
        self._pw = None
        self._browser = None
        self._wired: set[int] = set()
        self._last_nav: str | None = None

    async def start(self):
        from playwright.async_api import async_playwright  # noqa: PLC0415
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self._cdp_url)
        for ctx in self._browser.contexts:
            ctx.on("page", lambda p: asyncio.ensure_future(self._wire(p)))
            for page in ctx.pages:
                await self._wire(page)
        logger.info("hermes.teach_vnc.recorder.started session=%s", self._sid)

    async def _wire(self, page):
        pid = id(page)
        if pid in self._wired:
            return
        self._wired.add(pid)
        try:
            s = await page.context.new_cdp_session(page)
            await s.send("Runtime.enable")
            await s.send("Runtime.addBinding", {"name": "__lumenRec"})
            await s.send("Page.enable")
            await s.send("Page.addScriptToEvaluateOnNewDocument", {"source": OBSERVER_JS})
            s.on("Runtime.bindingCalled", self._on_binding)
            page.on("framenavigated", self._on_nav)
            try:
                await page.evaluate(OBSERVER_JS)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            logger.debug("hermes.teach_vnc.wire.skip", exc_info=True)

    def _on_nav(self, frame):
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
        except Exception:  # noqa: BLE001
            return
        if not url or url == self._last_nav or url == "about:blank":
            return
        self._last_nav = url
        self._capture({"type": "navigate", "url": url})

    def _on_binding(self, params):
        if params.get("name") != "__lumenRec":
            return
        try:
            self._capture(json.loads(params.get("payload", "{}")))
        except Exception:  # noqa: BLE001
            pass

    def _capture(self, ev):
        payload = _event_to_payload(ev)
        if not payload:
            return
        try:
            self._orch.capture_step(
                session_id=self._sid, surface_kind=SurfaceKind.BROWSER,
                action_payload=payload,
            )
        except Exception:  # noqa: BLE001
            logger.warning("hermes.teach_vnc.capture.FAILED", exc_info=True)

    async def stop(self):
        try:
            if self._pw is not None:
                await self._pw.stop()  # detach; do NOT close the shared browser
        except Exception:  # noqa: BLE001
            pass


def create_teach_vnc_router(orchestrator=None, db_path=None):
    from hermes.shell_server.cowork.training_live import (  # noqa: PLC0415
        _cdp_url,
        _parse_uuid,
        _try_ensure_browser_running,
        _verify_token,
    )

    router = APIRouter()
    _recorders: dict[str, _Recorder] = {}

    def _auth(request: Request):
        expected = getattr(request.app.state, "shell_webui_token", "")
        auth = request.headers.get("authorization", "")
        tok = auth[7:] if auth[:7].lower() == "bearer " else ""
        if not _verify_token(tok, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @router.post("/api/v1/teach/start")
    async def teach_start(request: Request) -> dict:
        _auth(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        skill = str((body or {}).get("skill_name", "")).strip() or "skill"
        sid = uuid4()
        # RECORDING session (no jailed /start — the browser is already up + shown via VNC)
        if orchestrator is not None:
            orchestrator.start(
                tenant_id=uuid4(), human_user_id=uuid4(), skill_id=skill,
                surface_kinds_allowed=frozenset(SurfaceKind), session_id=sid,
            )
        # DB row so /save's compile can look it up (mirror the training create).
        if db_path:
            try:
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        "INSERT INTO training_sessions "
                        "(session_id, skill_name, description, state, started_at, surface_kind) "
                        "VALUES (?,?,?,?,?,?)",
                        (str(sid), skill, None, "idle", now, "browser"),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                logger.warning("hermes.teach_vnc.db_insert.failed", exc_info=True)
        await _try_ensure_browser_running()
        rec = _Recorder(_cdp_url(), orchestrator, sid)
        try:
            await rec.start()
            _recorders[str(sid)] = rec
        except Exception:  # noqa: BLE001
            logger.warning("hermes.teach_vnc.recorder.start.failed", exc_info=True)
        return {"session_id": str(sid), "skill_name": skill}

    @router.post("/api/v1/teach/{session_id}/save")
    async def teach_save(session_id: str, request: Request) -> dict:
        _auth(request)
        sid = _parse_uuid(session_id)
        rec = _recorders.pop(session_id, None)
        if rec is not None:
            await rec.stop()
        # DB idle → review, orchestrator → REVIEWING, then compile+sign.
        if db_path:
            try:
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        "UPDATE training_sessions SET state='review', stopped_at=? "
                        "WHERE session_id=? AND state IN ('idle','capturing','paused')",
                        (now, str(sid)),
                    )
                    conn.commit()
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                logger.warning("hermes.teach_vnc.db_review.failed", exc_info=True)
        try:
            from hermes.shell_server.training.api import (  # noqa: PLC0415
                _transition_orchestrator_to_review,
            )
            _transition_orchestrator_to_review(orchestrator, sid)
        except Exception:  # noqa: BLE001
            logger.warning("hermes.teach_vnc.review.failed", exc_info=True)
        signed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        skill_name = "skill"
        if db_path:
            try:
                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT skill_name FROM training_sessions WHERE session_id=?",
                        (str(sid),),
                    ).fetchone()
                finally:
                    conn.close()
                if row and row[0]:
                    skill_name = str(row[0])
            except Exception:  # noqa: BLE001
                pass
        try:
            from hermes.shell_server.training.persist import (  # noqa: PLC0415
                compile_and_persist,
            )
            compile_and_persist(
                db_path=db_path, orchestrator=orchestrator, session_id=sid,
                skill_name=skill_name, signed_at=signed_at,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.teach_vnc.sign.failed", exc_info=True)
            raise HTTPException(status_code=409, detail=f"could not save skill: {exc}")
        return {"ok": True, "session_id": str(sid)}

    return router
