"""Tests markdown -> Pango markup."""

from __future__ import annotations

import pytest

from hermes.shell.presentation.gtk4.widgets.markdown_render import (
    render_markdown_to_pango,
)

pytestmark = pytest.mark.unit


class TestInline:
    def test_bold(self) -> None:
        blocks = render_markdown_to_pango("Hola **mundo**")
        assert "<b>mundo</b>" in blocks[0].content

    def test_italic(self) -> None:
        blocks = render_markdown_to_pango("Hola *mundo*")
        assert "<i>mundo</i>" in blocks[0].content

    def test_code_inline(self) -> None:
        blocks = render_markdown_to_pango("Run `ls -la`")
        assert "<tt>" in blocks[0].content
        assert "ls -la" in blocks[0].content

    def test_link(self) -> None:
        blocks = render_markdown_to_pango("[Hermes](https://hermes.ai)")
        assert '<a href="https://hermes.ai">Hermes</a>' in blocks[0].content


class TestEscape:
    def test_lt_gt_escaped(self) -> None:
        blocks = render_markdown_to_pango("if a < b > c")
        assert "&lt;" in blocks[0].content
        assert "&gt;" in blocks[0].content

    def test_amp_escaped(self) -> None:
        blocks = render_markdown_to_pango("foo & bar")
        assert "&amp;" in blocks[0].content


class TestBlocks:
    def test_code_block_separated(self) -> None:
        md = "Texto antes\n```python\nprint('hi')\n```\ntexto después"
        blocks = render_markdown_to_pango(md)
        assert len(blocks) == 3
        assert blocks[0].kind == "pango"
        assert blocks[1].kind == "code"
        assert blocks[1].language == "python"
        assert "print('hi')" in blocks[1].content
        assert blocks[2].kind == "pango"

    def test_bullets(self) -> None:
        md = "- uno\n- dos\n- tres"
        blocks = render_markdown_to_pango(md)
        text = blocks[0].content
        assert "• uno" in text
        assert "• dos" in text

    def test_headers(self) -> None:
        md = "# Title\n## Sub\n### Section"
        blocks = render_markdown_to_pango(md)
        text = blocks[0].content
        assert "x-large" in text
        assert "large" in text
        assert "weight=\"bold\"" in text


class TestEmpty:
    def test_empty_input(self) -> None:
        assert render_markdown_to_pango("") == [] or render_markdown_to_pango("")[0].content == ""
