"""native_teach — record a demonstration in the user's OWN (native) Chrome.

Why: the jailed headless browser cannot be shown to the user at native quality +
speed (CDP screencast is 1x/blurry; captureScreenshot is sharp but ~10 fps). For
TEACHING (the human demonstrates), the right model — used by Playwright `codegen`
and Chrome's DevTools Recorder — is to let the user drive their REAL browser and
just OBSERVE it: inject a small recorder script that reports clicks / inputs /
navigations (with a robust selector + semantic descriptor) and feed those into the
same TrainingSessionOrchestrator → SkillCompiler → SKILL.md pipeline.

Unlike training_live (where Lumen INJECTS the input from a canvas and records it),
here the user injects the input in their own Chrome, so we cannot record from our
own dispatch — we OBSERVE via an injected page recorder bound to Runtime.addBinding.

Transport: connect_over_cdp to the native Chrome's CDP endpoint (BROWSER_CDP_URL /
an explicit url). The agent's autonomous browsing stays in the jail; only teaching
uses the native browser (human-supervised).
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)

from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger("hermes.shell_server.cowork.native_teach")

# Injected into every page of the native browser. Capture-phase listeners so we see
# the event before the page can stopPropagation. Reports a compact JSON string via
# the __lumenRec binding (exposed by Runtime.addBinding).
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
      id: el.id||null, name: (el.getAttribute && el.getAttribute('name'))||null,
      selector: sel(el) };
  }
  function rec(o){ try { window.__lumenRec(JSON.stringify(o)); } catch(_){} }
  document.addEventListener('click', e => rec({type:'click', element:desc(e.target)}), true);
  document.addEventListener('change', e => rec({type:'input',
    value:((e.target&&e.target.value)||'').slice(0,500), element:desc(e.target)}), true);
})();
"""


def _ensure_orch_session(orchestrator, db_path, sid) -> None:
    """Ensure the orchestrator has a RECORDING session for `sid`.

    The native flow creates the DB row (POST /api/v1/training) but never runs /start
    (which is what calls orchestrator.start AND opens the jailed browser). So the
    orchestrator has no in-memory session yet → capture_step would raise 'unknown
    session'. Lazily create it here from the DB skill_name, no jailed surface.
    """
    if orchestrator is None or sid is None:
        return
    try:
        orchestrator.get_session(session_id=sid)
        return  # already exists
    except Exception:  # noqa: BLE001 — not found → create it below
        pass
    skill = "skill"
    if db_path:
        try:
            import sqlite3  # noqa: PLC0415
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT skill_name FROM training_sessions WHERE session_id=?",
                    (str(sid),),
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                skill = str(row[0])
        except Exception:  # noqa: BLE001
            pass
    try:
        orchestrator.start(
            tenant_id=uuid4(),
            human_user_id=uuid4(),
            skill_id=skill,
            surface_kinds_allowed=frozenset(SurfaceKind),
            session_id=sid,
        )
    except Exception:  # noqa: BLE001 — a concurrent request may have created it
        logger.debug("hermes.native_teach.ensure_session.race", exc_info=True)


def event_to_step_payload(ev: dict) -> dict | None:
    """Map an observed browser event → the capture_step action_payload shape.

    Shared by the in-container recorder (NativeTeachRecorder) AND the REST ingest
    used by the host-side recorder, so both produce identical steps.
    """
    kind = ev.get("type")
    if kind == "navigate":
        url = str(ev.get("url", "")).strip()
        return {"kind": "navigate", "url": url} if url else None
    el = ev.get("element") or {}
    if kind == "click":
        return {"kind": "act", "action": "click", "element": el}
    if kind == "input":
        return {"kind": "act", "action": "key",
                "text": str(ev.get("value", ""))[:500], "element": el}
    return None


class NativeTeachRecorder:
    """Observe a native Chrome over CDP and capture user actions as training steps.

    on_step(step_dict) is called for every captured step (also fed to the
    orchestrator) so the UI can show a live list of what was captured.
    """

    def __init__(self, *, cdp_url: str, orchestrator, session_id: "UUID | None",
                 on_step=None) -> None:
        self._cdp_url = cdp_url
        self._orch = orchestrator
        self._sid = session_id
        self._on_step = on_step
        self._pw = None
        self._browser = None
        self._wired: set[int] = set()
        self._last_nav: str | None = None
        self._running = False

    async def start(self) -> None:
        from playwright.async_api import async_playwright  # noqa: PLC0415

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self._cdp_url)
        self._running = True
        # Wire every existing page, and every page opened later (new tabs).
        for ctx in self._browser.contexts:
            ctx.on("page", lambda p: asyncio.ensure_future(self._wire_page(p)))
            for page in ctx.pages:
                await self._wire_page(page)
        # A brand-new browser may have no context yet; also listen at browser level.
        try:
            self._browser.on("page", lambda p: asyncio.ensure_future(self._wire_page(p)))
        except Exception:  # noqa: BLE001 — not all builds emit browser-level page
            pass
        logger.info("hermes.native_teach.started cdp=%s session=%s",
                    self._cdp_url, self._sid)

    async def _wire_page(self, page) -> None:
        pid = id(page)
        if pid in self._wired:
            return
        self._wired.add(pid)
        try:
            session = await page.context.new_cdp_session(page)
            await session.send("Runtime.enable")
            await session.send("Runtime.addBinding", {"name": "__lumenRec"})
            await session.send("Page.enable")
            await session.send("Page.addScriptToEvaluateOnNewDocument",
                               {"source": OBSERVER_JS})
            session.on("Runtime.bindingCalled", self._on_binding)
            page.on("framenavigated", self._on_framenav)
            # Install on the already-loaded document too.
            try:
                await page.evaluate(OBSERVER_JS)
            except Exception:  # noqa: BLE001 — page may be mid-navigation
                pass
        except Exception:  # noqa: BLE001 — a page may close mid-wiring
            logger.debug("hermes.native_teach.wire_page.skipped", exc_info=True)

    def _on_framenav(self, frame) -> None:
        # Only main-frame navigations, deduped.
        try:
            if frame.parent_frame is not None:
                return
            url = frame.url
        except Exception:  # noqa: BLE001
            return
        if not url or url == self._last_nav or url == "about:blank":
            return
        self._last_nav = url
        self._emit({"type": "navigate", "url": url})

    def _on_binding(self, params: dict) -> None:
        if params.get("name") != "__lumenRec":
            return
        try:
            ev = json.loads(params.get("payload", "{}"))
        except (ValueError, TypeError):
            return
        if ev.get("type") in ("click", "input"):
            self._emit(ev)

    def _emit(self, ui_event: dict) -> None:
        payload = event_to_step_payload(ui_event)
        if payload and self._orch is not None and self._sid is not None:
            try:
                self._orch.capture_step(
                    session_id=self._sid,
                    surface_kind=SurfaceKind.BROWSER,
                    action_payload=payload,
                )
            except Exception:  # noqa: BLE001 — surface, don't crash the recorder
                logger.warning("hermes.native_teach.capture_step.FAILED payload=%r",
                               payload, exc_info=True)
        if self._on_step is not None:
            try:
                self._on_step(ui_event)
            except Exception:  # noqa: BLE001
                pass

    async def stop(self) -> None:
        self._running = False
        try:
            if self._browser is not None:
                # Do NOT close the user's browser — just detach playwright.
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            logger.debug("hermes.native_teach.stop.ignored", exc_info=True)
        logger.info("hermes.native_teach.stopped session=%s", self._sid)


# ---------------------------------------------------------------------------
# WS router — /api/v1/training/{id}/native
# ---------------------------------------------------------------------------


def create_native_teach_router(orchestrator=None, db_path=None):
    """WS that records a demonstration from the user's NATIVE Chrome.

    The session must already be RECORDING (created via POST /api/v1/training, which
    calls orchestrator.start); this WS only runs the observer and streams captured
    steps to the client for a live list. Persisting is the existing POST …/sign.

    Query params: token (same webui bearer as training_live), cdp (the native
    Chrome CDP url, e.g. http://host.containers.internal:9222; the CLI sets it up).
    """
    import os  # noqa: PLC0415

    from hermes.shell_server.cowork.training_live import (  # noqa: PLC0415
        _cdp_url,
        _parse_uuid,
        _verify_token,
    )

    router = APIRouter()

    @router.post("/api/v1/training/{session_id}/native-step")
    async def native_step(session_id: str, request: Request) -> dict:
        """Ingest ONE observed step from the HOST-SIDE recorder → capture_step.

        The host recorder (runs on the user's Mac, connects to their local Chrome)
        POSTs each click/input/navigation here — no container→host networking, works
        the same on Mac and Linux. Auth: Authorization: Bearer <webui token> (same
        bearer the operator-token middleware already enforces on POST /api/v1/*).
        """
        expected: str = getattr(request.app.state, "shell_webui_token", "")
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth[:7].lower() == "bearer " else ""
        if not _verify_token(token, expected):
            raise HTTPException(status_code=401, detail="unauthorized")
        sid = _parse_uuid(session_id)
        try:
            ev = await request.json()
        except Exception:  # noqa: BLE001
            ev = {}
        payload = event_to_step_payload(ev if isinstance(ev, dict) else {})
        captured = False
        if payload and orchestrator is not None and sid is not None:
            _ensure_orch_session(orchestrator, db_path, sid)
            try:
                orchestrator.capture_step(
                    session_id=sid,
                    surface_kind=SurfaceKind.BROWSER,
                    action_payload=payload,
                )
                captured = True
            except Exception:  # noqa: BLE001 — surface, return ok:false-ish
                logger.warning("hermes.native_teach.native_step.FAILED session=%s",
                               session_id, exc_info=True)
        return {"ok": True, "captured": captured}

    @router.post("/api/v1/training/{session_id}/native-stop")
    async def native_stop(session_id: str, request: Request) -> dict:
        """Finish a native demonstration: DB idle/capturing/paused → review AND the
        orchestrator → REVIEWING, so POST …/sign compiles the SKILL.md. The native
        flow never runs /start (which opens the jailed browser), so the DB stays
        'idle'; this moves it straight to 'review' without any jailed surface.
        """
        expected: str = getattr(request.app.state, "shell_webui_token", "")
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth[:7].lower() == "bearer " else ""
        if not _verify_token(token, expected):
            raise HTTPException(status_code=401, detail="unauthorized")
        sid = _parse_uuid(session_id)
        moved = False
        if db_path:
            try:
                import datetime  # noqa: PLC0415
                import sqlite3  # noqa: PLC0415
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                conn = sqlite3.connect(db_path)
                try:
                    cur = conn.execute(
                        "UPDATE training_sessions SET state='review', stopped_at=? "
                        "WHERE session_id=? AND state IN ('idle','capturing','paused')",
                        (now, str(sid)),
                    )
                    moved = cur.rowcount > 0
                    conn.commit()
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                logger.warning("hermes.native_teach.native_stop.db_failed session=%s",
                               session_id, exc_info=True)
        try:
            from hermes.shell_server.training.api import (  # noqa: PLC0415
                _transition_orchestrator_to_review,
            )
            _transition_orchestrator_to_review(orchestrator, sid)
        except Exception:  # noqa: BLE001
            logger.warning("hermes.native_teach.native_stop.orch_failed session=%s",
                           session_id, exc_info=True)
        return {"ok": True, "moved_to_review": moved}

    @router.websocket("/api/v1/training/{session_id}/native")
    async def native_teach_ws(websocket: WebSocket, session_id: str) -> None:
        webui_token: str = getattr(websocket.app.state, "shell_webui_token", "")
        if not _verify_token(websocket.query_params.get("token", ""), webui_token):
            await websocket.close(code=1008, reason="unauthorized")
            return
        await websocket.accept()

        cdp = (
            websocket.query_params.get("cdp")
            or os.environ.get("NATIVE_BROWSER_CDP_URL")
            or _cdp_url()
        )
        sid = _parse_uuid(session_id)
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def on_step(ev: dict) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, ev)
            except Exception:  # noqa: BLE001
                pass

        rec = NativeTeachRecorder(
            cdp_url=cdp, orchestrator=orchestrator, session_id=sid, on_step=on_step
        )
        sender = receiver = None
        try:
            await rec.start()
            await websocket.send_json({"type": "ready", "cdp": cdp})

            async def _send() -> None:
                while True:
                    ev = await queue.get()
                    await websocket.send_json({"type": "step", "step": ev})

            async def _recv() -> None:
                # Drain client messages (e.g. keepalive); exits on disconnect.
                while True:
                    await websocket.receive_text()

            sender = asyncio.create_task(_send())
            receiver = asyncio.create_task(_recv())
            done, pending = await asyncio.wait(
                [sender, receiver], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("hermes.native_teach.ws.error session=%s", session_id)
        finally:
            if sender and not sender.done():
                sender.cancel()
            if receiver and not receiver.done():
                receiver.cancel()
            await rec.stop()

    return router
