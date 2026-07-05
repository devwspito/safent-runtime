"""CycleCdpContext — thread-local CDP URL scoping for per-cycle browser injection.

Problem being solved:
  browser_tool._get_cdp_override() reads os.environ["BROWSER_CDP_URL"] which is
  PROCESS-GLOBAL.  With N concurrent workers each running run_conversation() in
  different threads via run_in_executor, mutating os.environ per-cycle would let
  a Cerebro cycle's CDP URL leak into concurrent worker cycles (race condition).

Solution: thread-local storage.
  Each call to run_conversation() runs in a dedicated thread (default executor).
  We store the CDP URL in a threading.local() keyed to the thread.  A context
  manager sets the value for the Cerebro's thread BEFORE the lambda is invoked
  and clears it in the finally block.  Worker threads never set the value, so
  get() returns None for them — browser_tool falls back to its headless path.

Integration with browser_tool:
  browser_tool._get_cdp_override() is the hook we need to override.  When Nous
  is installed the function reads os.environ first, then config.yaml.  We install
  a module-level monkeypatch (install_thread_local_cdp_override) that makes it
  read the thread-local first, then fall back to os.environ / config.  The patch
  is installed ONCE at engine startup (idempotent, safe).

Why not os.environ:
  - Process-global: a write in thread A is visible to thread B immediately.
  - With a N-worker pool (default: RAM/16MB workers) Cerebro + workers can be
    concurrent → silent URL bleed into worker tools → worker uses headed browser
    instead of its isolated headless session (isolation violation).

Why not contextvars.ContextVar:
  - run_in_executor copies the current context into the thread (Python ≥ 3.7).
  - That would work too, BUT the contextvars copy is shallow: if Nous's internal
    coroutines or threads spawn sub-tasks they would NOT inherit the context.
  - threading.local() is simpler and fully sufficient here because run_conversation
    is synchronous and single-threaded per executor slot.

Capa: infrastructure (runtime wiring layer, no domain logic).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Generator

logger = logging.getLogger(__name__)

# Thread-local storage: each executor thread has its own _local.cdp_url value.
_local = threading.local()

_TOOL_MODULE = "tools.browser_tool"
_PATCHED_ATTR = "_hermes_cdp_patched"
_JAIL_BLOCK_ATTR = "_hermes_jail_block_patched"


def get_thread_cdp_url() -> str | None:
    """Return the CDP URL for the current thread, or None.

    Called by the monkeypatched _get_cdp_override in browser_tool — i.e. ONLY when
    a browser tool (browser_navigate/click/…) is about to run. The thread holds a
    LAZY PROVIDER (a callable): invoking it launches the Cerebro's headed browser
    on demand and returns its CDP URL. This is what makes the launch LAZY — for
    non-browser actions (calculator, terminal, files) _get_cdp_override is never
    called, so the provider never runs and NO Chromium is started.

    Returns None when the current thread is a worker (no provider set) or the
    provider fails (→ browser_tool falls back to its headless path).
    """
    provider = getattr(_local, "cdp_provider", None)
    if provider is None:
        return None
    try:
        return provider()
    except Exception:  # noqa: BLE001 — fail-soft: headless fallback
        logger.warning("hermes.cycle_cdp_context.provider_error", exc_info=True)
        return None


@contextmanager
def cerebro_cdp_scope(cdp_provider) -> Generator[None, None, None]:
    """Bind a LAZY CDP provider to the current thread for a Cerebro cycle.

    *cdp_provider* is a zero-arg callable returning ``str | None`` (the CDP URL,
    launching the browser on first call). Usage inside the run_in_executor lambda:

        with cerebro_cdp_scope(provider):
            agent.run_conversation(...)

    The provider is only INVOKED if the agent actually uses a browser tool (via
    the patched _get_cdp_override). Cleared in finally even on exception.
    """
    _local.cdp_provider = cdp_provider
    try:
        yield
    finally:
        _local.cdp_provider = None


def install_thread_local_cdp_override() -> bool:
    """Monkeypatch browser_tool._get_cdp_override to read thread-local first.

    Idempotent: returns True on first install, False if already patched.
    Fails silently (log + return False) if browser_tool is not installed —
    the engine still works, browser calls will fall back to headless.

    This function MUST be called once at engine init (before any run_cycle).
    """
    try:
        import importlib  # noqa: PLC0415
        bt = importlib.import_module(_TOOL_MODULE)
    except ModuleNotFoundError:
        logger.debug(
            "hermes.cycle_cdp_context.install: %s not installed — "
            "headed-browser override not wired (Nous not installed or CI mode)",
            _TOOL_MODULE,
        )
        return False

    if getattr(bt, _PATCHED_ATTR, False):
        return False  # already patched

    original = getattr(bt, "_get_cdp_override", None)

    def _patched_get_cdp_override() -> str | None:
        # Thread-local wins: this thread is running a Cerebro cycle.
        thread_val = get_thread_cdp_url()
        if thread_val is not None:
            return thread_val
        # Fallback: original logic (reads os.environ / config.yaml).
        if original is not None:
            return original()
        return None

    bt._get_cdp_override = _patched_get_cdp_override  # type: ignore[attr-defined]
    setattr(bt, _PATCHED_ATTR, True)
    logger.info(
        "hermes.cycle_cdp_context.install: browser_tool._get_cdp_override "
        "patched with thread-local CDP scope"
    )
    return True


def cleanup_thread_browser_session(task_id: str) -> None:
    """Tear down the per-task browser session browser_tool created for *task_id*.

    Why this exists (the confined-browser stability fix):
      Nous only reaps a browser session on its ERROR-exit paths (truncated tool
      call, thinking-budget exhausted, rollback) or after a 300 s inactivity
      timeout. On the HAPPY path of run_conversation the session — and with it
      the agent-browser controller daemon (``cdp_<hash>``) plus the CDP
      supervisor websocket — LEAKS. The vendored ``tools.browser_tool`` mints a
      FRESH random ``cdp_<hash>`` controller each time a cycle first touches the
      browser (its session name is a random ``uuid4``, NOT derived from our
      task_id). A leaked controller from cycle N then contends with cycle N+1's
      controller (both run ``Target.setAutoAttach`` on the SAME single jailed
      Chromium) → the second ``open`` deadlocks and times out after 60 s.
      *_run_conversation_streaming_or_fallback DOES pass task_id=cycle_task_id*,
      and browser_tool keys its reap map by that task_id, so calling
      ``cleanup_browser(cycle_task_id)`` here reaps this cycle's controller and
      keeps the next cycle clean. (Do NOT trust an older comment claiming we call
      run_conversation "without a task_id" — we do pass it; see nous_engine
      _run_conversation_streaming_or_fallback.)

      Root-cause note (deferred, out of repo scope): the churn itself — a fresh
      random ``cdp_<hash>`` per cycle instead of ONE reused session — lives in
      the vendored ``tools.browser_tool`` (``/usr/.../dist-packages/tools``),
      which this repo does not own. The proper fix (derive the agent-browser
      session from a stable task_id / reuse one controller in attach mode) must
      land there; this per-cycle reaper is the correct repo-side mitigation until
      then. It does NOT raise the concurrency cap (still one jailed Chromium).

    What it does NOT touch: the underlying jailed Chromium. That browser is a
    systemd-managed service (hermes-browser-launcher), not a browser_tool
    session — ``cleanup_browser`` only closes the agent-browser CLIENT session
    (its page/context), kills the controller daemon, and stops the supervisor.
    Chromium survives the client disconnect (verified: sequential attach/open
    against the same Chromium succeeds repeatedly).

    No-op when the cycle never used the browser (no session under *task_id*) or
    when Nous is not installed. Fail-soft: never raises into the cycle.
    """
    if not task_id:
        return
    try:
        import importlib  # noqa: PLC0415
        bt = importlib.import_module(_TOOL_MODULE)
    except ModuleNotFoundError:
        return
    cleanup = getattr(bt, "cleanup_browser", None)
    if cleanup is None:
        return
    try:
        cleanup(task_id)
    except Exception:  # noqa: BLE001 — reaping must never break the cycle
        logger.warning(
            "hermes.cycle_cdp_context.cleanup_browser_failed task=%s",
            task_id,
            exc_info=True,
        )


def install_jail_block_local_session() -> bool:
    """Monkeypatch browser_tool._create_local_session to block unconfined spawns.

    When HERMES_BROWSER_JAIL=1, replaces _create_local_session with a function
    that raises RuntimeError immediately. This is the fail-closed seatbelt: even
    if BROWSER_CDP_URL is unset, the tool can NEVER fall back to spawning a
    host-netns Chromium via a plain Popen.

    Idempotent (guard: _JAIL_BLOCK_ATTR). Fail-soft when Nous is not installed
    (log debug + return False). Does not raise at import time.

    Returns True on first install, False if already installed or jail inactive.
    """
    if os.environ.get("HERMES_BROWSER_JAIL", "1") != "1":
        return False

    try:
        import importlib  # noqa: PLC0415
        bt = importlib.import_module(_TOOL_MODULE)
    except ModuleNotFoundError:
        logger.debug(
            "hermes.cycle_cdp_context.jail_block: %s not installed — "
            "jail seatbelt not wired (Nous not installed or CI mode)",
            _TOOL_MODULE,
        )
        return False

    if getattr(bt, _JAIL_BLOCK_ATTR, False):
        return False  # already patched

    original_create = getattr(bt, "_create_local_session", None)
    if original_create is None:
        logger.debug(
            "hermes.cycle_cdp_context.jail_block: "
            "_create_local_session not found in %s — skipping seatbelt",
            _TOOL_MODULE,
        )
        return False

    def _jailed_create_local_session(*args, **kwargs):
        raise RuntimeError(
            "browser jail active: refusing to spawn an unconfined local "
            "browser session (HERMES_BROWSER_JAIL=1). "
            "Ensure the jailed browser is running and BROWSER_CDP_URL is set."
        )

    bt._create_local_session = _jailed_create_local_session  # type: ignore[attr-defined]
    setattr(bt, _JAIL_BLOCK_ATTR, True)
    logger.info(
        "hermes.cycle_cdp_context.jail_block: browser_tool._create_local_session "
        "patched — unconfined host-netns browser spawn blocked"
    )
    return True
