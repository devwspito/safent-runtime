"""Convierte markdown simple a Pango markup para Gtk.Label.

Subset soportado (suficiente para chat de agente):
  **bold**         -> <b>bold</b>
  *italic*         -> <i>italic</i>
  `code`           -> <tt><span bgcolor>code</span></tt>
  ```code blocks``` -> bloque mono separado (devuelto como bloque distinto)
  # Header         -> <span size="large" weight="bold">
  - bullet         -> • bullet
  [link](url)      -> <a href="url">link</a>

NO renderiza tablas ni HTML embebido. Sanitiza < > & en el body para
evitar Pango parsing errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

_INLINE_BOLD = re.compile(r"\*\*([^*]+?)\*\*")
_INLINE_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+?)\*(?!\*)")
_INLINE_CODE = re.compile(r"`([^`\n]+?)`")
_INLINE_LINK = re.compile(r"\[([^\]]+?)\]\(([^)]+?)\)")


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _apply_inline(text: str) -> str:
    """Aplica formato inline (bold, italic, code, link) sobre texto YA escapado."""
    # Code primero (para que no interfiera con bold/italic dentro de `).
    text = _INLINE_CODE.sub(
        lambda m: f'<tt><span background="#1F2532"> {_escape(m.group(1))} </span></tt>',
        text,
    )
    text = _INLINE_LINK.sub(
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text
    )
    text = _INLINE_BOLD.sub(r"<b>\1</b>", text)
    text = _INLINE_ITALIC.sub(r"<i>\1</i>", text)
    return text


@dataclass(slots=True)
class MarkdownBlock:
    """Un bloque del documento renderizado."""

    kind: str  # 'pango' | 'code'
    content: str
    language: str | None = None


def iter_blocks(markdown_text: str) -> Iterator[MarkdownBlock]:
    """Itera por bloques. Code blocks se devuelven separados (kind='code')."""
    lines = markdown_text.split("\n")
    buffer: list[str] = []
    in_code = False
    code_lang: str | None = None
    code_buffer: list[str] = []

    def flush_buffer() -> Iterator[MarkdownBlock]:
        nonlocal buffer
        if buffer:
            yield MarkdownBlock(kind="pango", content=_render_lines(buffer))
            buffer = []

    def flush_code() -> Iterator[MarkdownBlock]:
        nonlocal code_buffer, code_lang
        if code_buffer:
            yield MarkdownBlock(
                kind="code",
                content="\n".join(code_buffer),
                language=code_lang,
            )
            code_buffer = []
            code_lang = None

    for line in lines:
        if line.startswith("```"):
            if in_code:
                yield from flush_code()
                in_code = False
            else:
                yield from flush_buffer()
                in_code = True
                code_lang = line[3:].strip() or None
            continue
        if in_code:
            code_buffer.append(line)
            continue
        buffer.append(line)

    yield from flush_buffer()
    yield from flush_code()


def _render_lines(lines: list[str]) -> str:
    """Renderiza un grupo de líneas no-code a Pango markup."""
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if not line:
            out.append("")
            continue
        stripped = line.lstrip()
        # Headers (# = h1, ## = h2, ### = h3).
        if stripped.startswith("# "):
            content = _apply_inline(_escape(stripped[2:]))
            out.append(
                f'<span size="x-large" weight="bold">{content}</span>'
            )
            continue
        if stripped.startswith("## "):
            content = _apply_inline(_escape(stripped[3:]))
            out.append(
                f'<span size="large" weight="bold">{content}</span>'
            )
            continue
        if stripped.startswith("### "):
            content = _apply_inline(_escape(stripped[4:]))
            out.append(f'<span weight="bold">{content}</span>')
            continue
        # Bullets.
        if stripped.startswith(("- ", "* ")):
            content = _apply_inline(_escape(stripped[2:]))
            out.append(f"  • {content}")
            continue
        # Numeric list.
        m = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if m:
            content = _apply_inline(_escape(m.group(2)))
            out.append(f"  {m.group(1)}. {content}")
            continue
        # Texto plano.
        out.append(_apply_inline(_escape(stripped)))
    return "\n".join(out)


def render_markdown_to_pango(markdown_text: str) -> list[MarkdownBlock]:
    """API pública: devuelve lista de bloques renderizados."""
    return list(iter_blocks(markdown_text))
