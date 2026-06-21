"""ChatPane — the Cerebro conversation. The heart of Lumen Terminal.

Streams the agent's reply token-by-token over the reused TaskStreamClient
(enqueue → stream_path → frames: delta / thinking_delta / tool_call / done).
Thinking and tool calls render inline so the operator sees the agent *work*,
not just a final blob — the thing that makes an agent TUI feel alive.

Slash commands:
  /help           — list available slash commands
  /attach <path>  — stage a file path to be appended to the next message
  /mcp            — jump to the MCP pane
  /skills         — jump to the Skills pane
  /integraciones  — jump to the Integrations pane (Composio)
  /integrations   — alias for /integraciones
  /composio       — alias for /integraciones
  /agentes        — jump to the Agents pane
  /agents         — alias for /agentes
  /tareas         — jump to the Tasks pane
  /tasks          — alias for /tareas
  /seguridad      — jump to the Security pane
  /security       — alias for /seguridad
  /programador    — jump to the Scheduler pane
  /memoria        — jump to the Memory pane
  /memory         — alias for /memoria
  /proveedores    — jump to the Providers pane
  /providers      — alias for /proveedores
  /paquetes       — jump to the Packages pane
"""

from __future__ import annotations

import asyncio
import os

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Input, Markdown, Static

from hermes.tui.bridge import BridgeError, new_conversation_id
from hermes.tui.theme import PALETTE


class ChatMessage(Vertical):
    """One bubble: a role label + a Markdown body that grows as tokens arrive."""

    def __init__(
        self,
        role_label: str,
        *,
        css_class: str,
        role_style: str,
        initial_text: str = "",
    ) -> None:
        super().__init__(classes=f"msg {css_class}")
        self._role_label = role_label
        self._role_style = role_style
        self._buf = initial_text

    def compose(self):
        label = Text(self._role_label, style=f"bold {self._role_style}")
        yield Static(label, classes="msg-role")
        yield Markdown(self._buf, classes="msg-body")

    def append(self, delta: str) -> None:
        self._buf += delta
        if self.is_mounted:
            self.query_one(Markdown).update(self._buf)

    def set_text(self, text: str) -> None:
        self._buf = text
        if self.is_mounted:
            self.query_one(Markdown).update(self._buf)

    @property
    def text(self) -> str:
        return self._buf


class StatusLine(Static):
    """Ephemeral italic line for thinking / tool-call status."""

    def __init__(self, text: str, *, css_class: str) -> None:
        super().__init__(text, classes=f"msg {css_class}")


# Slash commands that navigate to a pane (cmd → pane_id).
_SLASH_NAV: dict[str, str] = {
    "mcp": "mcp",
    "skills": "skills",
    "integraciones": "integrations",
    "integrations": "integrations",
    "composio": "integrations",
    "agentes": "agents",
    "agents": "agents",
    "tareas": "tasks",
    "tasks": "tasks",
    "seguridad": "security",
    "security": "security",
    "programador": "scheduler",
    "memoria": "memory",
    "memory": "memory",
    "proveedores": "providers",
    "providers": "providers",
    "paquetes": "packages",
}


class ChatPane(Vertical):
    PANE_ID = "chat"

    def __init__(self) -> None:
        super().__init__(id="pane-chat")
        self._conversation_id = new_conversation_id()
        self._streaming = False
        self._active_agent_msg: ChatMessage | None = None
        self._got_text = False
        self._stream_end: asyncio.Event | None = None
        self._thinking_line: StatusLine | None = None
        self._thinking_buf = ""
        self._pending_attach: str | None = None  # path staged by /attach

    @property
    def bridge(self):
        return self.app.bridge  # type: ignore[attr-defined]

    def compose(self):
        with VerticalScroll(id="chat-log"):
            yield self._welcome()
        with Vertical(id="composer"):
            yield Input(
                placeholder="Escribe a Cerebro…  (Enter envía · /help · /attach <ruta>)",
                id="prompt",
            )
            yield Static("", classes="hint", id="composer-hint")

    def _welcome(self) -> Widget:
        return ChatMessage(
            "Cerebro",
            css_class="msg-agent",
            role_style=PALETTE["teal"],
            initial_text=(
                "Soy **Lumen**, el cerebro de este sistema. Opero el equipo, el "
                "navegador y las apps por ti. Pídeme algo y me pongo a ello.\n\n"
                "Escribe `/help` para los comandos rápidos (`/mcp`, `/skills`, "
                "`/integraciones`, `/seguridad`…) o `/attach <ruta>` para adjuntar "
                "un archivo a tu mensaje.\n\n"
                "_Las acciones sensibles te las confirmo con una tarjeta antes de "
                "ejecutarlas; toda instalación pasa por el centro de seguridad._"
            ),
        )

    async def activate(self) -> None:
        self.query_one("#prompt", Input).focus()

    def new_conversation(self) -> None:
        self._conversation_id = new_conversation_id()
        self._pending_attach = None
        log = self.query_one("#chat-log", VerticalScroll)
        log.remove_children()
        log.mount(self._welcome())
        self.query_one("#prompt", Input).focus()
        self._set_hint("")
        self.notify("Conversación nueva", timeout=2)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return
        text = event.value.strip()
        if not text or self._streaming:
            return
        event.input.value = ""
        if text.startswith("/"):
            await self._handle_slash(text)
            return
        await self._send(text)

    # -- slash commands ---------------------------------------------------

    async def _handle_slash(self, text: str) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        parts = text[1:].split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        echo = ChatMessage("Tú", css_class="msg-user", role_style=PALETTE["amber"], initial_text=text)
        await log.mount(echo)
        log.scroll_end(animate=False)

        # Navigation commands — jump to pane and confirm inline.
        if cmd in _SLASH_NAV:
            pane_id = _SLASH_NAV[cmd]
            self.app.go_to(pane_id)  # type: ignore[attr-defined]
            await self._render_system(f"Navegando a **{cmd}**…")
            return

        # /attach <path>
        if cmd == "attach":
            await self._handle_attach(arg)
            return

        # Data-query commands.
        data_handlers = {
            "help": self._slash_help,
            "mcp": self._slash_mcp,
            "skills": self._slash_skills,
            "providers": self._slash_providers,
            "agents": self._slash_agents,
            "tasks": self._slash_tasks,
            "security": self._slash_security,
            "memory": self._slash_memory,
        }
        handler = data_handlers.get(cmd)
        if handler is None:
            await self._render_system(f"Comando `/{cmd}` desconocido. Prueba `/help`.")
            return
        try:
            md = await handler()
        except Exception as exc:  # noqa: BLE001
            md = f"No se pudo ejecutar `/{cmd}`: {exc}"
        await self._render_system(md)

    async def _handle_attach(self, path: str) -> None:
        if not path:
            await self._render_system(
                "Uso: `/attach <ruta>`  — adjunta un archivo al siguiente mensaje.\n\n"
                "Ejemplo: `/attach /home/user/factura.pdf`"
            )
            return
        expanded = os.path.expanduser(path)
        if not os.path.exists(expanded):
            await self._render_system(
                f"No se encuentra el archivo: `{path}`\n\n"
                "_Revisa la ruta e inténtalo de nuevo._"
            )
            return
        self._pending_attach = expanded
        self._set_hint(f"Adjunto: {expanded}  (se enviará con tu próximo mensaje)")
        await self._render_system(
            f"Adjunto preparado: `{expanded}`\n\n"
            "_Escribe tu mensaje y lo enviaré junto con la ruta del archivo._"
        )

    async def _render_system(self, markdown: str) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        msg = ChatMessage(
            "Lumen",
            css_class="msg-agent",
            role_style=PALETTE["teal"],
            initial_text=markdown,
        )
        await log.mount(msg)
        log.scroll_end(animate=False)

    async def _slash_help(self) -> str:
        return (
            "**Comandos de navegación** (van directamente a la sección)\n\n"
            "- `/mcp` — servidores MCP\n"
            "- `/skills` — skills del agente\n"
            "- `/integraciones` — conexiones Composio\n"
            "- `/agentes` — agentes\n"
            "- `/tareas` — actividad reciente\n"
            "- `/seguridad` — centro de seguridad\n"
            "- `/programador` — tareas programadas\n"
            "- `/memoria` — memoria del agente\n"
            "- `/proveedores` — proveedores LLM\n"
            "- `/paquetes` — gestor de paquetes\n\n"
            "**Comandos de consulta** (responden aquí en el chat)\n\n"
            "- `/help` — esta ayuda\n"
            "- `/attach <ruta>` — adjunta un archivo al siguiente mensaje\n\n"
            "_Para todo lo demás, háblame normal y me pongo a ello._"
        )

    async def _slash_mcp(self) -> str:
        servers = await self.bridge.list_mcp_servers()
        if not servers:
            return "No hay **servidores MCP** conectados. Ve a MCP (4) para añadir uno."
        rows = "\n".join(
            f"- **{s.get('name', '—')}** · {s.get('status') or s.get('health') or '—'}"
            f" · {s.get('tool_count') or (len(s['tools']) if isinstance(s.get('tools'), list) else '—')} tools"
            for s in servers
        )
        return f"**Servidores MCP** ({len(servers)})\n\n{rows}"

    async def _slash_skills(self) -> str:
        skills = await self.bridge.list_skills()
        if not skills:
            return "No tengo **skills** todavía. Las aprendo con el uso, o instálalas desde Skills (2)."
        rows = "\n".join(
            f"- **{s.get('name', '—')}** · {s.get('state') or s.get('status') or '—'}" for s in skills
        )
        return f"**Skills** ({len(skills)})\n\n{rows}"

    async def _slash_providers(self) -> str:
        provs = await self.bridge.list_providers()
        active = await self.bridge.get_active_provider()
        am = active.get("model") or active.get("name") or "sin modelo"
        if not provs:
            return f"Modelo activo: **{am}**\n\nNo hay proveedores. Añade uno en Proveedores."
        rows = "\n".join(
            f"- {p.get('name') or p.get('provider') or '—'} · {p.get('model') or '—'}" for p in provs
        )
        return f"Modelo activo: **{am}**\n\n**Proveedores** ({len(provs)})\n\n{rows}"

    async def _slash_agents(self) -> str:
        agents = await self.bridge.list_agents()
        active = await self.bridge.get_active_agent()
        rows = "\n".join(
            f"- {'♛ ' if a.get('is_default') else ''}**{a.get('name', '—')}**"
            f"{' · activo' if str(a.get('id')) == active else ''}"
            f" — {a.get('role', '') or '—'}"
            for a in agents
        )
        return f"**Agentes** ({len(agents)})\n\n{rows}"

    async def _slash_tasks(self) -> str:
        tasks = await self.bridge.list_recent_tasks(20)
        if not tasks:
            return "Sin **actividad** reciente."
        rows = "\n".join(
            f"- {t.get('title') or t.get('kind') or '—'} · {t.get('status') or '—'}"
            f" · {t.get('created_at') or ''}" for t in tasks
        )
        return f"**Actividad reciente** ({len(tasks)})\n\n{rows}"

    async def _slash_security(self) -> str:
        policy = await self.bridge.get_security_policy()
        audit = await self.bridge.get_audit_chain_head()
        scans = await self.bridge.list_recent_scans(10)
        integ = audit.get("integrity", "desconocida")
        pol = "\n".join(f"- {k}: {v}" for k, v in policy.items()) or "_sin política_"
        sc = "\n".join(
            f"- {s.get('verdict', '—')} · {s.get('kind', '')} · {s.get('identifier', '')}"
            for s in scans
        ) or "_sin escaneos recientes_"
        return (
            f"**Centro de seguridad**\n\n"
            f"Cadena de auditoría: **{integ}**\n\n"
            f"**Política**\n{pol}\n\n"
            f"**Escaneos recientes**\n{sc}\n\n"
            "_Toda instalación pasa por aquí: escaneo → score → decides._"
        )

    async def _slash_memory(self) -> str:
        mem = await self.bridge.list_memory(20)
        if not mem:
            return "No tengo nada en **memoria** todavía."
        rows = "\n".join(
            f"- ({m.get('target', '—')}) {str(m.get('content') or m.get('content_truncated') or '')[:80]}"
            for m in mem
        )
        return f"**Memoria** ({len(mem)})\n\n{rows}"

    async def _send(self, text: str) -> None:
        # Append staged attachment path to the message text.
        if self._pending_attach:
            text = f"{text}\n\n[Adjunto: {self._pending_attach}]"
            self._pending_attach = None
            self._set_hint("")

        log = self.query_one("#chat-log", VerticalScroll)
        user = ChatMessage(
            "Tú", css_class="msg-user", role_style=PALETTE["amber"], initial_text=text
        )
        await log.mount(user)
        log.scroll_end(animate=False)
        self._streaming = True
        self._active_agent_msg = None
        self._got_text = False
        self._stream_end = asyncio.Event()
        self._set_hint("Cerebro está pensando…")
        self.run_worker(self._drive(text), exclusive=True, group="chat")

    async def _drive(self, text: str) -> None:
        """Orchestrate one turn.

        Text streams token-by-token via the ChatDelta D-Bus signals (the reliable
        path; the app forwards them to on_chat_delta_signal). The task-stream is
        read best-effort only for thinking/tool-call status. If the signals deliver
        no text by stream-end, fall back to the persisted reply (store) so the
        answer is never lost.
        """
        log = self.query_one("#chat-log", VerticalScroll)
        try:
            _task_id, stream_path = await self.bridge.enqueue_chat(
                text, self._conversation_id
            )
        except BridgeError as exc:
            await self._fail(f"No se pudo enviar: {exc}")
            return

        prior_assistant = await self._assistant_count()
        # Best-effort thinking/tool-call display (text comes via signals).
        self.run_worker(self._read_aux(stream_path), group="chat-aux")

        # Wait for the stream-end signal (or a generous timeout for LLM+tools).
        try:
            await asyncio.wait_for(self._stream_end.wait(), timeout=90)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass

        # Fallback: signals delivered no text → fetch the persisted reply.
        if not self._got_text:
            reply = await self._fetch_reply(prior_assistant)
            if reply:
                self._clear_thinking()
                if self._active_agent_msg is None:
                    self._active_agent_msg = ChatMessage(
                        "Cerebro", css_class="msg-agent",
                        role_style=PALETTE["teal"], initial_text=reply,
                    )
                    await log.mount(self._active_agent_msg)
                else:
                    self._active_agent_msg.set_text(reply)
                self._got_text = True
                log.scroll_end(animate=False)

        self._clear_thinking()
        self._streaming = False
        self._set_hint("")
        if not self._got_text:
            await self._fail("Cerebro no devolvió respuesta.")

    async def _read_aux(self, stream_path: str) -> None:
        """Best-effort: render thinking/tool-call frames from the task-stream."""
        try:
            async for frame in self.bridge.stream(stream_path):
                if frame.kind == "thinking_delta":
                    self._append_thinking(frame.payload.get("delta", ""))
                elif frame.kind == "tool_call":
                    await self._show_tool(frame.payload)
                elif frame.kind in ("error", "done"):
                    break
        except Exception:  # noqa: BLE001
            pass

    # -- live streaming via ChatDelta signals (forwarded by the app) ------

    def on_chat_delta_signal(self, conversation_id: str, text: str) -> None:
        if conversation_id != self._conversation_id or not self._streaming or not text:
            return
        log = self.query_one("#chat-log", VerticalScroll)
        if self._active_agent_msg is None:
            self._clear_thinking()
            self._active_agent_msg = ChatMessage(
                "Cerebro", css_class="msg-agent", role_style=PALETTE["teal"]
            )
            log.mount(self._active_agent_msg)
        self._active_agent_msg.append(text)
        self._got_text = True
        log.scroll_end(animate=False)

    def on_chat_stream_end_signal(self, conversation_id: str) -> None:
        if conversation_id == self._conversation_id and self._stream_end is not None:
            self._stream_end.set()

    _ASSISTANT_ROLES = ("assistant", "agent", "cerebro", "hermes")

    @staticmethod
    def _assistant_msgs(detail: dict) -> list[dict]:
        msgs = detail.get("messages") or detail.get("turns") or []
        return [m for m in msgs if str(m.get("role") or m.get("author") or "").lower()
                in ChatPane._ASSISTANT_ROLES]

    async def _assistant_count(self) -> int:
        try:
            detail = await self.bridge.get_conversation(self._conversation_id)
        except Exception:  # noqa: BLE001
            return 0
        return len(self._assistant_msgs(detail))

    async def _fetch_reply(self, prior_count: int) -> str:
        """Poll the conversation store until a NEW assistant reply appears."""
        for _ in range(45):
            await asyncio.sleep(1.0)
            try:
                detail = await self.bridge.get_conversation(self._conversation_id)
            except Exception:  # noqa: BLE001
                continue
            assistants = self._assistant_msgs(detail)
            if len(assistants) > prior_count:
                txt = str(assistants[-1].get("content") or assistants[-1].get("text") or "").strip()
                if txt:
                    return txt
        return ""

    def _append_thinking(self, delta: str) -> None:
        if not delta:
            return
        log = self.query_one("#chat-log", VerticalScroll)
        if self._thinking_line is None:
            self._thinking_buf = ""
            self._thinking_line = StatusLine("⋯ pensando…", css_class="msg-thinking")
            log.mount(self._thinking_line)
        self._thinking_buf = (self._thinking_buf + delta)[-200:]
        self._thinking_line.update(Text(f"⋯ {self._thinking_buf}", style="italic"))
        log.scroll_end(animate=False)

    def _clear_thinking(self) -> None:
        if self._thinking_line is not None:
            self._thinking_line.remove()
            self._thinking_line = None
        self._thinking_buf = ""

    async def _show_tool(self, payload: dict) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        name = payload.get("name") or payload.get("tool") or "herramienta"
        line = StatusLine(f"⚒ usando {name}", css_class="msg-tool")
        await log.mount(line)
        log.scroll_end(animate=False)

    async def _fail(self, message: str) -> None:
        log = self.query_one("#chat-log", VerticalScroll)
        line = StatusLine(f"✕ {message}", css_class="msg-tool")
        await log.mount(line)
        self._clear_thinking()
        self._streaming = False
        self._set_hint("")
        self.notify(message, severity="error", timeout=6)

    def _set_hint(self, text: str) -> None:
        try:
            self.query_one("#composer-hint", Static).update(text)
        except Exception:  # noqa: BLE001
            pass
