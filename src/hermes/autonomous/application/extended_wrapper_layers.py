"""Extended Wrapper Layers — BLOCKER C-7 / FR-064 (T110, threat-model THR-45).

6 sub-capas adicionales para vectores difíciles:
  (a) side_effecting_paths: regex per SiteSpec bloqueados aunque sean GET.
  (b) CSP script-src 'self': vía context.set_extra_http_headers en preview.
  (c) postMessage interceptor: JS injection que monkey-patcha window.postMessage.
  (d) Prefetch (link rel=prefetch): bloqueado vía route resource_type==preload.
  (e) Form submit JS-triggered: bloqueado vía init_script que sobrescribe
      HTMLFormElement.prototype.submit.
  (f) Service Worker fetch: interceptado vía CDP Network.setBlockedURLs.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Route

logger = logging.getLogger(__name__)

# CSP header para preview mode — bloquea scripts externos (THR-45 (c) JSONP).
_PREVIEW_CSP_HEADER = "script-src 'self'; object-src 'none'; base-uri 'none'"

# JS para interceptar y bloquear window.postMessage (THR-45 (e)).
_POST_MESSAGE_INTERCEPTOR_JS = """
(function() {
  const _origPostMessage = window.postMessage.bind(window);
  window.postMessage = function(message, targetOrigin, transfer) {
    if (window.__hermesPreviewMode) {
      console.warn('[Hermes Preview] postMessage blocked:', JSON.stringify(message).slice(0, 100));
      return;
    }
    return _origPostMessage(message, targetOrigin, transfer);
  };
  window.__hermesPreviewMode = true;
})();
"""

# JS para bloquear HTMLFormElement.prototype.submit (THR-45 (f)).
_FORM_SUBMIT_BLOCKER_JS = """
(function() {
  const _origSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function() {
    if (window.__hermesPreviewMode) {
      console.warn('[Hermes Preview] form.submit() blocked');
      return;
    }
    return _origSubmit.call(this);
  };
})();
"""


class SideEffectingPathsBlocker:
    """(a) Bloquea GETs a paths marcados como side-effecting en el SiteSpec."""

    def __init__(
        self,
        *,
        patterns: Sequence[str],
        on_blocked: Callable[[str, str], None] | None = None,
    ) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._on_blocked = on_blocked

    async def attach(self, context: "BrowserContext") -> None:
        await context.route("**/*", self._handle)

    async def _handle(self, route: "Route") -> None:
        url = route.request.url
        for pattern in self._patterns:
            if pattern.search(url):
                logger.warning(
                    "replay_preview_side_effecting_path_blocked",
                    extra={"url": url},
                )
                if self._on_blocked:
                    self._on_blocked("side_effecting_path", url)
                await route.abort("blockedbyclient")
                return
        await route.continue_()


class CspInjector:
    """(b) Aplica CSP restrictiva vía extra HTTP headers en preview mode."""

    async def attach(self, context: "BrowserContext") -> None:
        await context.set_extra_http_headers(
            {"Content-Security-Policy": _PREVIEW_CSP_HEADER}
        )
        logger.info("csp_injector_attached", extra={"csp": _PREVIEW_CSP_HEADER})


class PostMessageInterceptor:
    """(c) Monkey-patcha window.postMessage vía add_init_script."""

    async def attach(self, page: "Page") -> None:
        await page.add_init_script(_POST_MESSAGE_INTERCEPTOR_JS)
        logger.info("post_message_interceptor_attached")


class PrefetchBlocker:
    """(d) Bloquea prefetch/preload vía route con resource_type check."""

    def __init__(self, *, on_blocked: Callable[[str], None] | None = None) -> None:
        self._on_blocked = on_blocked

    async def attach(self, context: "BrowserContext") -> None:
        await context.route("**/*", self._handle)

    async def _handle(self, route: "Route") -> None:
        resource_type = route.request.resource_type
        if resource_type in ("prefetch", "preload", "manifest"):
            url = route.request.url
            logger.info(
                "replay_preview_prefetch_blocked",
                extra={"url": url, "resource_type": resource_type},
            )
            if self._on_blocked:
                self._on_blocked(url)
            await route.abort("blockedbyclient")
        else:
            await route.continue_()


class FormSubmitBlocker:
    """(e) Bloquea form.submit() JS-triggered vía init_script."""

    async def attach(self, page: "Page") -> None:
        await page.add_init_script(_FORM_SUBMIT_BLOCKER_JS)
        logger.info("form_submit_blocker_attached")


class ServiceWorkerBlocker:
    """(f) Bloquea Service Worker fetch vía route interception.

    Los service workers registrados en el sitio target pueden interceptar
    fetches y ejecutar mutaciones. Bloqueamos el recurso service-worker.js.
    """

    _SW_PATTERNS = ("service-worker.js", "sw.js", "serviceworker.js", "/sw/")

    def __init__(self, *, on_blocked: Callable[[str], None] | None = None) -> None:
        self._on_blocked = on_blocked

    async def attach(self, context: "BrowserContext") -> None:
        await context.route("**/*", self._handle)

    async def _handle(self, route: "Route") -> None:
        url = route.request.url.lower()
        for pattern in self._SW_PATTERNS:
            if pattern in url:
                logger.info(
                    "replay_preview_service_worker_blocked",
                    extra={"url": route.request.url},
                )
                if self._on_blocked:
                    self._on_blocked(route.request.url)
                await route.abort("blockedbyclient")
                return
        await route.continue_()


class ExtendedWrapperLayers:
    """Agrupa las 6 sub-capas adicionales del wrapper read-only.

    Instanciar con SiteSpec del tenant para configurar side_effecting_paths.
    """

    def __init__(
        self,
        *,
        side_effecting_patterns: Sequence[str] = (),
        on_blocked: Callable[[str, str], None] | None = None,
    ) -> None:
        self._path_blocker = SideEffectingPathsBlocker(
            patterns=side_effecting_patterns, on_blocked=on_blocked
        )
        self._csp = CspInjector()
        self._post_message = PostMessageInterceptor()
        self._prefetch = PrefetchBlocker(
            on_blocked=lambda url: on_blocked("prefetch", url) if on_blocked else None
        )
        self._form_submit = FormSubmitBlocker()
        self._service_worker = ServiceWorkerBlocker(
            on_blocked=lambda url: on_blocked("service_worker", url) if on_blocked else None
        )
        self._activated = False

    async def attach_context(self, context: "BrowserContext") -> None:
        """Aplica capas que se instalan a nivel de BrowserContext."""
        await self._path_blocker.attach(context)
        await self._csp.attach(context)
        await self._prefetch.attach(context)
        await self._service_worker.attach(context)

    async def attach_page(self, page: "Page") -> None:
        """Aplica capas que se instalan a nivel de Page."""
        await self._post_message.attach(page)
        await self._form_submit.attach(page)
        self._activated = True

    @property
    def is_activated(self) -> bool:
        return self._activated
