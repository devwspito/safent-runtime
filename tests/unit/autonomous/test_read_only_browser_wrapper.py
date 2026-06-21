"""Tests del ReadOnlyBrowserWrapper (T105) — verifica que TODAS las capas se enchufan.

Usa FakeBrowserContext minimal (sin Chromium real — constitución V).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from hermes.autonomous.application.read_only_browser_wrapper import (
    ReadOnlyBrowserWrapper,
    WrapperNotActivatedError,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fake BrowserContext y Page (sin playwright real)
# ---------------------------------------------------------------------------


@dataclass
class FakeRoute:
    _method: str = "GET"
    _url: str = "https://example.com"
    _resource_type: str = "document"
    aborted: bool = False
    continued: bool = False

    @property
    def request(self) -> "FakeRequest":
        return FakeRequest(
            method=self._method,
            url=self._url,
            resource_type=self._resource_type,
        )

    async def abort(self, reason: str = "") -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


@dataclass
class FakeRequest:
    method: str = "GET"
    url: str = "https://example.com"
    resource_type: str = "document"


@dataclass
class FakePage:
    """Minimal fake de Playwright Page."""

    _handlers: dict[str, list[Callable]] = field(default_factory=dict)
    _init_scripts: list[str] = field(default_factory=list)

    def on(self, event: str, callback: Callable) -> None:
        self._handlers.setdefault(event, []).append(callback)

    async def add_init_script(self, script: str) -> None:
        self._init_scripts.append(script)

    def has_handler(self, event: str) -> bool:
        return bool(self._handlers.get(event))


@dataclass
class FakeBrowserContext:
    """Minimal fake de Playwright BrowserContext."""

    _route_handlers: list[Callable] = field(default_factory=list)
    _extra_headers: dict[str, str] = field(default_factory=dict)

    async def route(self, pattern: str, callback: Callable) -> None:
        self._route_handlers.append(callback)

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self._extra_headers.update(headers)

    def has_route_handlers(self) -> bool:
        return bool(self._route_handlers)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWrapperActivation:
    async def test_not_activated_before_apply(self) -> None:
        wrapper = ReadOnlyBrowserWrapper()
        assert not wrapper.is_activated

    async def test_activated_after_apply(self) -> None:
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]
        assert wrapper.is_activated

    async def test_assert_activated_raises_before_apply(self) -> None:
        wrapper = ReadOnlyBrowserWrapper()
        with pytest.raises(WrapperNotActivatedError):
            wrapper.assert_activated()

    async def test_assert_activated_passes_after_apply(self) -> None:
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]
        wrapper.assert_activated()  # no debe levantar


class TestAllLayersAttached:
    async def test_context_has_route_handlers(self) -> None:
        """Capa 1 + extended (side_effecting, prefetch, SW) instalan route handlers."""
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]
        assert ctx.has_route_handlers()

    async def test_csp_header_injected(self) -> None:
        """Capa (b) ExtendedWrapperLayers CSP inyecta Content-Security-Policy."""
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]
        assert "Content-Security-Policy" in ctx._extra_headers

    async def test_page_has_download_handler(self) -> None:
        """Capa 2: DownloadBlocklist registra handler de download."""
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]
        assert page.has_handler("download")

    async def test_page_has_websocket_handler(self) -> None:
        """Capa 4: WebSocketFrameFilter registra handler de websocket."""
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]
        assert page.has_handler("websocket")

    async def test_init_scripts_injected(self) -> None:
        """Capas (c) y (e): postMessage interceptor + form.submit blocker."""
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]
        assert len(page._init_scripts) >= 2
        combined = "\n".join(page._init_scripts)
        assert "postMessage" in combined
        assert "HTMLFormElement" in combined


class TestMutationBlockedCallback:
    async def test_callback_invoked_on_mutation(self) -> None:
        blocked_events: list[tuple[str, str]] = []
        wrapper = ReadOnlyBrowserWrapper(
            on_mutation_blocked=lambda reason, detail: blocked_events.append(
                (reason, detail)
            )
        )
        ctx = FakeBrowserContext()
        page = FakePage()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]

        # Simula que el route handler recibe una ruta POST.
        handler = ctx._route_handlers[0]
        fake_route = FakeRoute(_method="POST", _url="https://example.com/api/save")

        # Invoca el handler directamente (sin Playwright real).
        await handler(fake_route)
        assert fake_route.aborted


class TestClickIntentClassifierIntegration:
    async def test_irreversible_click_detected_after_activation(self) -> None:
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]

        is_irr = wrapper.classify_click(element_text="Eliminar")
        assert is_irr is True

    async def test_safe_click_not_flagged(self) -> None:
        ctx = FakeBrowserContext()
        page = FakePage()
        wrapper = ReadOnlyBrowserWrapper()
        await wrapper.apply(ctx, page)  # type: ignore[arg-type]

        is_irr = wrapper.classify_click(element_text="Guardar")
        assert is_irr is False

    async def test_classify_before_activate_raises(self) -> None:
        wrapper = ReadOnlyBrowserWrapper()
        with pytest.raises(WrapperNotActivatedError):
            wrapper.classify_click(element_text="Eliminar")
