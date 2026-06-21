"""Tests ThinkBlockFilter (stream-aware <think> filtering)."""

from __future__ import annotations

import pytest

from hermes.shell_server.chat.think_block_filter import ThinkBlockFilter

pytestmark = pytest.mark.unit


class TestSingleChunk:
    def test_no_think_passes_through(self) -> None:
        f = ThinkBlockFilter()
        # "Hola mundo" tiene 10 chars, retain=7, safe_emit="Hol"
        out = f.process("Hola mundo")
        assert out == [("delta", "Hol")]
        out2 = f.flush()
        assert out2 == [("delta", "a mundo")]

    def test_full_think_block(self) -> None:
        f = ThinkBlockFilter()
        out = f.process("<think>razonamiento</think>respuesta")
        out.extend(f.flush())
        thinking = "".join(t for k, t in out if k == "thinking_delta")
        normal = "".join(t for k, t in out if k == "delta")
        assert thinking == "razonamiento"
        assert normal == "respuesta"

    def test_only_think_block(self) -> None:
        f = ThinkBlockFilter()
        out = f.process("<think>solo razonamiento</think>")
        assert out == [("thinking_delta", "solo razonamiento")]
        assert f.flush() == []


class TestStreaming:
    def test_partial_open_tag(self) -> None:
        f = ThinkBlockFilter()
        # Chunk 1: "Hola <thi" — '<thi' es tag parcial, debe retener.
        out = f.process("Hola <thi")
        # len buffer 9, retain 7 → emite 'Ho'
        assert ("delta", "Ho") in out
        # Chunk 2: "nk>razonando</think>"
        out2 = f.process("nk>razonando</think>")
        # Buffer ahora "la <think>razonando</think>"
        # Encuentra <think> en idx 3 → emit "la " + entra thinking
        # Encuentra </think> → emit "razonando"
        assert ("delta", "la ") in out2
        assert ("thinking_delta", "razonando") in out2

    def test_partial_close_tag(self) -> None:
        f = ThinkBlockFilter()
        f.process("<think>razonan")
        # Chunk: "do</thi" — '</thi' es parcial al final.
        out = f.process("do</thi")
        # Buffer: "do</thi" len 7, retain 7 → emite ""
        # Pero 'do' ya estaba antes... el buffer tras process tiene
        # menos. Revisemos: tras process("<think>razonan"), buffer
        # tiene "razonan" en thinking mode (no found </think>),
        # safe_emit "" (retain 7), buffer queda "razonan".
        # process("do</thi"): buffer = "razonando</thi" (len 14).
        # find </think> → -1. retain 7 → safe_emit = "razonan" (chars 0..7).
        # Emite ("thinking_delta", "razonan"). Buffer = "do</thi".
        assert ("thinking_delta", "razonan") in out
        # Chunk: "nk>fin"
        out2 = f.process("nk>fin")
        # Buffer = "do</think>fin" → encuentra </think> idx 2
        # Emite ("thinking_delta", "do") + cambio normal + buffer="fin"
        # "fin" len 3 < retain 7 → buffer queda "fin", no emite.
        assert ("thinking_delta", "do") in out2
        assert f.flush() == [("delta", "fin")]

    def test_text_split_across_many_chunks(self) -> None:
        f = ThinkBlockFilter()
        text = "antes<think>pensando</think>despues"
        all_out: list = []
        for ch in text:  # un char a la vez
            all_out.extend(f.process(ch))
        all_out.extend(f.flush())

        normal = "".join(t for k, t in all_out if k == "delta")
        thinking = "".join(t for k, t in all_out if k == "thinking_delta")
        assert normal == "antesdespues"
        assert thinking == "pensando"


class TestMultipleBlocks:
    def test_two_think_blocks(self) -> None:
        f = ThinkBlockFilter()
        out = f.process("a<think>x</think>b<think>y</think>c")
        out.extend(f.flush())
        normal = "".join(t for k, t in out if k == "delta")
        thinking = "".join(t for k, t in out if k == "thinking_delta")
        assert normal == "abc"
        assert thinking == "xy"


class TestImplicitThinking:
    def test_implicit_thinking_naked_close(self) -> None:
        """Qwen 3 / vLLM emite reasoning sin <think> + cierra con </think>."""
        f = ThinkBlockFilter(start_in_thinking=True)
        out = f.process("Analizando...\nrespuesta esperada</think>Soy Qwen.")
        out.extend(f.flush())
        thinking = "".join(t for k, t in out if k == "thinking_delta")
        normal = "".join(t for k, t in out if k == "delta")
        assert "Analizando" in thinking
        assert normal == "Soy Qwen."

    def test_implicit_thinking_only(self) -> None:
        f = ThinkBlockFilter(start_in_thinking=True)
        out = f.process("razonando sin cierre")
        out.extend(f.flush())
        thinking = "".join(t for k, t in out if k == "thinking_delta")
        assert thinking == "razonando sin cierre"


class TestFlush:
    def test_flush_pending_in_normal(self) -> None:
        f = ThinkBlockFilter()
        f.process("corto")
        out = f.flush()
        assert out == [("delta", "corto")]

    def test_flush_pending_in_thinking(self) -> None:
        f = ThinkBlockFilter()
        out1 = f.process("<think>incompleto")
        out2 = f.flush()
        thinking = "".join(t for k, t in out1 + out2 if k == "thinking_delta")
        assert thinking == "incompleto"

    def test_flush_empty(self) -> None:
        f = ThinkBlockFilter()
        assert f.flush() == []
