"""Lightweight liveness probe for the jailed agent browser.

Answers ONE question for the UI: does the shared jailed Chromium currently have a
REAL (non-blank) page open? This is the HONEST source of truth for the "El agente
está usando el navegador / Abrir En vivo" chip and the "En vivo" view — it replaces
keying off tool NAMES, which lie:

  - a browser_* that fell to an invisible headless session or failed to launch
    (e.g. the old socket-dir EACCES) opened no real page → chip must stay OFF;
  - web_search / web_extract are egress, never touch the browser → chip OFF;
  - the eager boot-time about:blank is not a real page → chip OFF until a real
    navigation happens.

Cheap: a single HTTP GET to the DevTools ``/json/list`` endpoint of the jailed
CDP (no Playwright connect), TTL-cached so a frequent status poll cannot hammer
CDP. Fail-soft: any error (browser down / unreachable / timeout) → False. The
daemon already reaches this CDP over the veth (see cowork.watch_live); this probe
uses the same fixed internal address.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

_DEFAULT_CDP_URL = "http://10.200.0.2:9333"
_PROBE_TTL_S: float = 2.0
_PROBE_TIMEOUT_S: float = 0.3

# Best-effort process-local cache: {"at": monotonic, "live": bool}. A tiny race
# between threads only costs a redundant probe — no lock needed for a hint.
_cache: dict[str, float | bool] = {"at": 0.0, "live": False}


def _cdp_base() -> str:
    return os.environ.get("BROWSER_CDP_URL", _DEFAULT_CDP_URL)


def _is_real_url(url: str) -> bool:
    """A page counts as 'real' only if it is not a blank/internal chrome URL."""
    return bool(url) and not url.startswith(
        ("about:", "chrome:", "chrome-extension:", "devtools:")
    )


def agent_browser_live() -> bool:
    """True iff the jailed browser has a real (non-blank) page target open.

    TTL-cached (2s). Fail-soft → False. Safe to call from any thread; the D-Bus
    status read runs it in an executor so it never blocks the event loop.
    """
    now = time.monotonic()
    last_at = _cache["at"]
    if isinstance(last_at, (int, float)) and now - last_at < _PROBE_TTL_S:
        return bool(_cache["live"])

    live = False
    try:
        url = _cdp_base().rstrip("/") + "/json/list"
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT_S) as resp:  # noqa: S310 — fixed internal veth host
            targets = json.loads(resp.read().decode("utf-8", "replace"))
        live = any(
            isinstance(t, dict)
            and t.get("type") == "page"
            and _is_real_url(str(t.get("url", "")))
            for t in (targets or [])
        )
    except Exception:  # noqa: BLE001 — best-effort; unreachable/down/timeout → not live
        live = False

    _cache["at"] = now
    _cache["live"] = live
    return live
