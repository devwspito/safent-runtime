#!/usr/bin/env python3
"""LumenSO Shell — la shell NATIVA del SO (no una máscara sobre GNOME).

GTK4 + WebKit6 a pantalla completa cargando la UI premium de LumenSO
(lumenso.html / onboarding.html). Puentea JS ↔ daemon por D-Bus REAL
(org.hermes.Runtime1): el provider se configura desde el onboarding, el chat
va por Enqueue/GetConversation. CERO HTTP, cero hardcode, cero chromium-jaula.

Se ejecuta como la sesión gráfica (reemplaza gnome-shell). El compositor (mutter
en modo kiosk, o el de hermes/lumen) lanza SOLO esto fullscreen.

Bridge JS:
    window.webkit.messageHandlers.hermes.postMessage(JSON.stringify({id, method, args}))
  → Python llama al daemon por D-Bus → responde con:
    window.__hermesReply(id, resultJSON)
La UI usa el helper window.hermes(method, args) -> Promise (inyectado abajo).
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import GLib, Gtk, WebKit  # noqa: E402

UI_DIR = os.environ.get("LUMENSO_UI_DIR", "/usr/share/lumenso")
HOME_URI = f"file://{UI_DIR}/lumenso.html"
ONBOARD_URI = f"file://{UI_DIR}/onboarding.html"

DBUS_NAME = "org.hermes.Runtime"
DBUS_PATH = "/org/hermes/Runtime"
DBUS_IFACE = "org.hermes.Runtime1"


class DaemonBridge:
    """Puente asíncrono al daemon Hermes por D-Bus (dbus_fast en un hilo propio)."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._iface = None
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _iface_get(self):
        if self._iface is not None:
            return self._iface
        from dbus_fast import BusType  # noqa: PLC0415
        from dbus_fast.aio import MessageBus  # noqa: PLC0415

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        intro = await bus.introspect(DBUS_NAME, DBUS_PATH)
        obj = bus.get_proxy_object(DBUS_NAME, DBUS_PATH, intro)
        self._iface = obj.get_interface(DBUS_IFACE)
        return self._iface

    async def _dispatch(self, method: str, args: dict):
        iface = await self._iface_get()
        if method == "add_provider":
            return await iface.call_add_provider(json.dumps(args.get("draft", args)))
        if method == "test_provider":
            return await iface.call_test_provider(args["provider_id"])
        if method == "list_providers":
            return await iface.call_list_providers()
        if method == "get_active_provider":
            return await iface.call_get_active_provider()
        if method == "set_active_provider":
            return await iface.call_set_active_provider(args["provider_id"])
        if method == "enqueue":
            return await iface.call_enqueue(
                args.get("trigger_kind", "chat_message"),
                args["text"], int(args.get("priority", 0)),
                args.get("dedup_key", ""), args.get("conversation_id", ""),
                args.get("operator_token", ""),
            )
        if method == "get_conversation":
            return await iface.call_get_conversation(args["conversation_id"])
        if method == "pause":
            return await iface.call_pause(args.get("task_id", ""))
        if method == "resume":
            return await iface.call_resume(args.get("task_id", ""))
        raise ValueError(f"método desconocido: {method}")

    def call(self, method: str, args: dict, done) -> None:
        """Programa la llamada D-Bus; `done(ok: bool, payload)` vuelve en el hilo GTK."""
        async def runner():
            try:
                result = await self._dispatch(method, args)
                GLib.idle_add(done, True, result)
            except Exception as exc:  # noqa: BLE001 — el error vuelve a la UI, no crashea
                GLib.idle_add(done, False, repr(exc))

        asyncio.run_coroutine_threadsafe(runner(), self._loop)


_BOOTSTRAP_JS = """
window.__hermesPending = {};
window.__hermesSeq = 0;
window.hermes = function(method, args){
  return new Promise(function(resolve, reject){
    var id = ++window.__hermesSeq;
    window.__hermesPending[id] = {resolve: resolve, reject: reject};
    window.webkit.messageHandlers.hermes.postMessage(
      JSON.stringify({id: id, method: method, args: args || {}}));
  });
};
window.__hermesReply = function(id, ok, payload){
  var p = window.__hermesPending[id]; if(!p) return;
  delete window.__hermesPending[id];
  ok ? p.resolve(payload) : p.reject(payload);
};
"""


class LumenShell(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="com.lumenso.Shell")
        self.bridge = DaemonBridge()

    def do_activate(self) -> None:
        win = Gtk.ApplicationWindow(application=self)
        win.fullscreen()
        win.set_title("LumenSO")

        ucm = WebKit.UserContentManager()
        ucm.add_script(
            WebKit.UserScript.new(
                _BOOTSTRAP_JS,
                WebKit.UserContentInjectedFrames.ALL_FRAMES,
                WebKit.UserScriptInjectionTime.START, None, None,
            )
        )
        ucm.register_script_message_handler("hermes", None)
        ucm.connect("script-message-received::hermes", self._on_message)

        self.webview = WebKit.WebView(user_content_manager=ucm)
        # ¿Hay provider activo? → onboarding si no, escritorio si sí.
        self.bridge.call("get_active_provider", {}, self._route_first_screen)
        win.set_child(self.webview)
        win.present()

    def _route_first_screen(self, ok: bool, payload) -> bool:
        has_provider = ok and payload and payload not in ("", "null", "{}", "none")
        self.webview.load_uri(HOME_URI if has_provider else ONBOARD_URI)
        return False

    def _on_message(self, _ucm, value) -> None:
        try:
            msg = json.loads(value.to_json(0))
        except Exception:
            msg = json.loads(value.to_string())
        rid = msg.get("id")
        method = msg.get("method")
        args = msg.get("args", {})

        def done(ok: bool, payload) -> bool:
            js = "window.__hermesReply(%d, %s, %s);" % (
                rid, "true" if ok else "false", json.dumps(payload),
            )
            self.webview.evaluate_javascript(js, -1, None, None, None, None)
            return False

        self.bridge.call(method, args, done)


if __name__ == "__main__":
    LumenShell().run(None)
