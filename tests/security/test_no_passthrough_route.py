"""T034 🔒 — Verifica AUSENCIA del passthrough LiteLLM en shell-server.

G6 / CTRL-P1-26 / SC-004 / CWE-862:
  0 rutas que esquiven el agente.

Verifica por AUSENCIA:
  1. No existe ruta POST /api/v1/chat que invoque litellm.acompletion directamente.
  2. No existe WS /ws/chat/{conv_id} que invoque litellm.acompletion directamente.
  3. El dict _conversations ya no existe en el módulo main (fue eliminado por T055).
  4. POST /api/v1/chat devuelve {task_id, stream_path} — no session_id / ws_url.

Estos tests FALLAN antes de T048+T055 y PASAN después.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_MAIN_PY = Path(__file__).parent.parent.parent / "src/hermes/shell_server/main.py"


# ---------------------------------------------------------------------------
# Helpers de AST — análisis estático sin importar el módulo
# ---------------------------------------------------------------------------


def _source() -> str:
    return _MAIN_PY.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source())


def _names_defined_at_module_level(tree: ast.Module) -> set[str]:
    """Devuelve los nombres de variables definidas a nivel de módulo."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _function_source(tree: ast.Module, func_name: str) -> str | None:
    """Devuelve el source de una función de primer nivel o None."""
    source_lines = _source().splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            if node.name == func_name:
                start = node.lineno - 1
                end = node.end_lineno
                return "".join(source_lines[start:end])
    return None


def _contains_call(func_source: str, symbol: str) -> bool:
    """True si el source de la función contiene una llamada al símbolo dado."""
    return symbol in func_source


# ---------------------------------------------------------------------------
# T034a — _conversations dict eliminado (T055)
# ---------------------------------------------------------------------------


class TestNoConversationsDict:
    def test_conversations_dict_absent_from_module_level(self) -> None:
        """`_conversations` ya no existe como variable de módulo.

        CTRL-P1-26: eliminar el dict in-memory de conversaciones.
        Antes de T055 falla; después pasa.
        """
        tree = _tree()
        names = _names_defined_at_module_level(tree)
        assert "_conversations" not in names, (
            "_conversations dict encontrado a nivel de módulo en main.py. "
            "Debe eliminarse (T055 / CTRL-P1-26 / G6)."
        )


# ---------------------------------------------------------------------------
# T034b — WS /ws/chat passthrough eliminado (T055)
# ---------------------------------------------------------------------------


class TestNoWsChatHandler:
    def test_ws_chat_handler_absent(self) -> None:
        """El handler `chat_stream` (WS /ws/chat) no existe.

        CTRL-P1-26: eliminar /ws/chat passthrough que invoca stream_completion
        directamente (litellm.acompletion). Antes de T055 falla; después pasa.
        """
        tree = _tree()
        handler_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                handler_names.add(node.name)
        assert "chat_stream" not in handler_names, (
            "Handler 'chat_stream' (WS /ws/chat passthrough) encontrado en main.py. "
            "Debe eliminarse (T055 / CTRL-P1-26 / G6)."
        )

    def test_no_stream_completion_import_in_main(self) -> None:
        """stream_completion de litellm_bridge no se importa en main.py.

        Después de T055, el import de `stream_completion` desaparece.
        """
        src = _source()
        assert "stream_completion" not in src, (
            "'stream_completion' encontrado en main.py. "
            "Debe eliminarse (T055 / CTRL-P1-26 / G6)."
        )


# ---------------------------------------------------------------------------
# T034c — POST /api/v1/chat responde con task_id + stream_path (T048)
# ---------------------------------------------------------------------------


class TestChatStartResponseShape:
    def test_chat_start_response_has_task_id_field(self) -> None:
        """ChatStartResponse debe declarar `task_id`, no `conversation_id` + ws_url.

        Después de T048, la respuesta cambia a {task_id, stream_path}.
        """
        from hermes.shell_server.main import ChatStartResponse  # noqa: PLC0415

        fields = ChatStartResponse.model_fields
        assert "task_id" in fields, (
            "ChatStartResponse debe tener campo 'task_id' (T048 / FR-010)."
        )
        assert "stream_path" in fields, (
            "ChatStartResponse debe tener campo 'stream_path' (T048 / FR-010)."
        )

    def test_chat_start_response_no_ws_url_field(self) -> None:
        """ChatStartResponse ya no tiene el campo `ws_url` del passthrough."""
        from hermes.shell_server.main import ChatStartResponse  # noqa: PLC0415

        fields = ChatStartResponse.model_fields
        assert "ws_url" not in fields, (
            "ChatStartResponse no debe tener campo 'ws_url' — era del passthrough. "
            "Debe eliminarse (T055 / CTRL-P1-26 / G6)."
        )


# ---------------------------------------------------------------------------
# T034d — No hay import directo de litellm.acompletion ni litellm_bridge
#         en el path caliente de chat_start (after T055)
# ---------------------------------------------------------------------------


class TestNoLitellmDirectCall:
    def test_chat_start_does_not_import_litellm_bridge_call(self) -> None:
        """La función `chat_start` no contiene llamadas a litellm_bridge.

        Después de T048+T055, chat_start delega al control_plane client,
        nunca a litellm directamente.
        """
        tree = _tree()
        chat_start_src = _function_source(tree, "chat_start")
        assert chat_start_src is not None, (
            "Función chat_start no encontrada en main.py."
        )
        assert not _contains_call(chat_start_src, "stream_completion"), (
            "chat_start contiene llamada a stream_completion (litellm). "
            "Debe delegar al control_plane (T048 / CTRL-P1-26 / G6)."
        )
        assert not _contains_call(chat_start_src, "acompletion"), (
            "chat_start contiene llamada a acompletion (litellm). "
            "Debe delegar al control_plane (T048 / CTRL-P1-26 / G6)."
        )
