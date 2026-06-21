"""Capa 1: HTTP Method Blocklist (T106, research §7 capa 1).

Bloquea PUT/POST/PATCH/DELETE vía Playwright context.route().
Emite evento on_mutation_blocked cuando intercepta.

Lazy-import de playwright para cumplir constitución V.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Request, Route

logger = logging.getLogger(__name__)

_BLOCKED_METHODS = frozenset({"PUT", "POST", "PATCH", "DELETE"})


class HttpMethodBlocklist:
    """Intercepta y aborta métodos HTTP mutadores en preview mode.

    Se instala sobre un BrowserContext vía ``attach(context)``.
    El callback ``on_blocked`` recibe (method, url) al bloquear.
    """

    def __init__(
        self,
        *,
        on_blocked: Callable[[str, str], None] | None = None,
        extra_blocked_methods: frozenset[str] = frozenset(),
    ) -> None:
        self._blocked = _BLOCKED_METHODS | extra_blocked_methods
        self._on_blocked = on_blocked
        self._active = False

    async def attach(self, context: "BrowserContext") -> None:
        """Registra el route handler en el BrowserContext."""
        await context.route("**/*", self._handle_route)
        self._active = True
        logger.info("http_method_blocklist_attached")

    async def _handle_route(self, route: "Route") -> None:
        method = route.request.method.upper()
        if method in self._blocked:
            url = route.request.url
            logger.warning(
                "replay_preview_http_blocked",
                extra={"method": method, "url": url},
            )
            if self._on_blocked:
                self._on_blocked(method, url)
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    @property
    def is_active(self) -> bool:
        return self._active
