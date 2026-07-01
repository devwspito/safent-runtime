"""SemanticToolIndex — intent-based retrieval of connected-integration tools.

Composio/MCP integrations expose MANY tools (gmail alone ~63; 10 integrations →
hundreds). Dumping them all into the agent's tool array bloats context and trips
progressive tool disclosure, so a weak model never finds the one it needs (it must
call ``tool_search`` and often doesn't). This index embeds each connected tool once
(name + description) with a small multilingual ONNX model (fastembed, CPU, offline)
and, given the user's INTENT for the current turn, returns the top-K most relevant
tools — so the agent sees a handful of RELEVANT tools directly, not hundreds.

Cross-lingual by design: the model (paraphrase-multilingual-MiniLM) matches a Spanish
intent ("leer mi correo") against English tool descriptions ("GMAIL_FETCH_EMAILS").

Fail-soft: if the embedder is unavailable (model not baked, import error) or anything
throws, retrieval returns None and the caller keeps the full set (prior behaviour).
Never raises into the agent cycle.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger("hermes.runtime.semantic_tool_index")

# Small, multilingual, CPU-friendly (~220MB). Overridable for tests / tuning.
_MODEL_NAME = os.environ.get(
    "HERMES_TOOL_EMBED_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)

# Baked model cache. The Containerfile pre-downloads the ONNX model to
# /var/lib/fastembed (OUTSIDE the runtime volume, mirroring the trivy-DB pattern
# so an upgrade over an existing volume never shadows it), and the hermes-runtime
# unit sets this env + adds the path to ReadWritePaths. Empty → fastembed's own
# default (dev/test download path).
_CACHE_DIR = os.environ.get("HERMES_TOOL_EMBED_CACHE") or None


class SemanticToolIndex:
    """Embed connected tools once; retrieve the top-K relevant to a query."""

    def __init__(
        self, *, model_name: str = _MODEL_NAME, cache_dir: str | None = _CACHE_DIR
    ) -> None:
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._model: Any = None          # lazy fastembed TextEmbedding
        self._model_failed = False
        self._vecs: dict[str, Any] = {}  # cache key -> unit vector (np.ndarray)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _embedder(self) -> Any:
        if self._model is not None or self._model_failed:
            return self._model
        with self._lock:
            if self._model is not None or self._model_failed:
                return self._model
            try:
                from fastembed import TextEmbedding  # noqa: PLC0415

                kwargs: dict[str, Any] = {"model_name": self._model_name}
                if self._cache_dir:
                    kwargs["cache_dir"] = self._cache_dir
                self._model = TextEmbedding(**kwargs)
                logger.info(
                    "hermes.semantic_tool_index.model_loaded model=%s cache_dir=%s",
                    self._model_name, self._cache_dir or "(default)",
                )
            except Exception as exc:  # noqa: BLE001
                self._model_failed = True
                logger.warning(
                    "hermes.semantic_tool_index.model_unavailable: %s — "
                    "semantic tool retrieval DISABLED (full tool set kept)",
                    exc,
                )
            return self._model

    @staticmethod
    def _doc(spec: Any) -> str:
        name = getattr(spec, "name", "") or ""
        desc = getattr(spec, "description", "") or ""
        return f"{name}: {desc}".strip()[:400]

    @staticmethod
    def _key(spec: Any) -> str:
        return f"{getattr(spec, 'name', '')}|{hash(getattr(spec, 'description', '') or '')}"

    @staticmethod
    def _unit(vec: Any) -> Any:
        import numpy as np  # noqa: PLC0415

        arr = np.asarray(vec, dtype="float32")
        norm = float(np.linalg.norm(arr))
        return arr / norm if norm else arr

    def _ensure_indexed(self, specs: list) -> bool:
        model = self._embedder()
        if model is None:
            return False
        missing = [s for s in specs if self._key(s) not in self._vecs]
        if missing:
            try:
                vecs = list(model.embed([self._doc(s) for s in missing]))
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.semantic_tool_index.embed_failed: %s", exc)
                return False
            for s, v in zip(missing, vecs):
                self._vecs[self._key(s)] = self._unit(v)
        return True

    def retrieve(self, query: str, specs: list, *, k: int = 12) -> "list | None":
        """Top-k specs most relevant to `query`. None → caller keeps the full set.

        Returns the full list unchanged when it is already <= k (no need to filter).
        """
        if not specs or not query:
            return None
        if len(specs) <= k:
            return list(specs)
        model = self._embedder()
        if model is None or not self._ensure_indexed(specs):
            return None
        try:
            import numpy as np  # noqa: PLC0415

            qv = self._unit(list(model.embed([query]))[0])
            scored = []
            for s in specs:
                v = self._vecs.get(self._key(s))
                if v is not None:
                    scored.append((float(np.dot(qv, v)), s))
            scored.sort(key=lambda t: t[0], reverse=True)
            top = [s for _, s in scored[:k]]
            logger.info(
                "hermes.semantic_tool_index.retrieved query_len=%d pool=%d -> %d",
                len(query), len(specs), len(top),
            )
            return top or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.semantic_tool_index.retrieve_failed: %s", exc)
            return None
