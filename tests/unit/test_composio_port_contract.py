"""T009 — safent_composio's public surface must match contracts/composio_port.pyi.

The stub in `tests/unit/contracts/composio_port.pyi` is a local, hand-synced
copy of the Enterprise repo's `specs/002-conexiones-e-integraciones/contracts/
composio_port.pyi` (spec 002 research.md, Decision 1: "una implementación,
nunca copiada"). It is parsed with `ast` — never imported — so this test has
no runtime dependency on the stub's own (illustrative, unimplemented) types.

This test only asserts a SUBSET relationship: every name/parameter the
contract requires must exist in `safent_composio`. It does not forbid
`safent_composio` from exposing more (e.g. `ToolInfo`, `list_tools`,
`execute_action` — runtime-only, not part of the Enterprise port).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest
import safent_composio as sc

pytestmark = pytest.mark.unit

_STUB_PATH = Path(__file__).parent / "contracts" / "composio_port.pyi"

_DATACLASS_NAMES = (
    "ToolkitInfo",
    "ConnectedAccountInfo",
    "ConnectionInitResult",
    "AuthConfigInfo",
)

_CLIENT_METHOD_NAMES = (
    "__init__",
    "list_toolkits",
    "assert_oauth_simple",
    "initiate_connection",
    "get_connected_account",
    "list_connected_accounts",
    "delete_connection",
    "validate_auth_config",
)


def _parse_stub() -> ast.Module:
    return ast.parse(_STUB_PATH.read_text())


def _find_classdef(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"contract stub has no class {name!r}")


def _annotated_field_names(node: ast.ClassDef) -> list[str]:
    return [
        stmt.target.id
        for stmt in node.body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]


def _find_method(node: ast.ClassDef, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name:
            return stmt
    raise AssertionError(f"contract stub's {node.name} has no method {name!r}")


@dataclasses.dataclass(frozen=True)
class _ParamBuckets:
    """Parameters grouped by how a caller is allowed to pass them.

    Bucketing (rather than a flat name set) catches a regression a flat set
    would miss: a contract parameter that still EXISTS but moved to a kind
    that breaks the calling convention the contract promised (e.g.
    keyword-only -> positional-only, which breaks every `foo(x=...)` caller).
    """

    positional_only: frozenset[str]
    positional_or_keyword: frozenset[str]
    keyword_only: frozenset[str]


def _stub_param_buckets(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> _ParamBuckets:
    args = fn.args
    return _ParamBuckets(
        positional_only=frozenset(a.arg for a in args.posonlyargs),
        positional_or_keyword=frozenset(a.arg for a in args.args if a.arg != "self"),
        keyword_only=frozenset(a.arg for a in args.kwonlyargs),
    )


def _real_param_buckets(sig: inspect.Signature) -> _ParamBuckets:
    by_kind: dict[inspect._ParameterKind, set[str]] = {
        inspect.Parameter.POSITIONAL_ONLY: set(),
        inspect.Parameter.POSITIONAL_OR_KEYWORD: set(),
        inspect.Parameter.KEYWORD_ONLY: set(),
    }
    for name, param in sig.parameters.items():
        if name == "self" or param.kind not in by_kind:
            continue
        by_kind[param.kind].add(name)
    return _ParamBuckets(
        positional_only=frozenset(by_kind[inspect.Parameter.POSITIONAL_ONLY]),
        positional_or_keyword=frozenset(by_kind[inspect.Parameter.POSITIONAL_OR_KEYWORD]),
        keyword_only=frozenset(by_kind[inspect.Parameter.KEYWORD_ONLY]),
    )


def _calling_conventions_dropped(stub: _ParamBuckets, real: _ParamBuckets) -> dict[str, set[str]]:
    """Contract parameters real no longer honours for their promised calling
    convention. `positional_or_keyword` in the contract must stay callable
    BOTH ways, so only a real `positional_or_keyword` satisfies it."""
    callable_positionally = real.positional_only | real.positional_or_keyword
    callable_by_keyword = real.keyword_only | real.positional_or_keyword
    dropped = {
        "positional_only": stub.positional_only - callable_positionally,
        "positional_or_keyword": stub.positional_or_keyword - real.positional_or_keyword,
        "keyword_only": stub.keyword_only - callable_by_keyword,
    }
    return {kind: missing for kind, missing in dropped.items() if missing}


class TestValueObjectsMatchContract:
    @pytest.mark.parametrize("name", _DATACLASS_NAMES)
    def test_dataclass_fields_match(self, name: str) -> None:
        """Every contract field must be present — but not necessarily alone.

        `safent_composio` ships extra fields the Enterprise port doesn't need
        (`ToolkitInfo.auth_schemes`, `AuthConfigInfo.status`): Ads-companion
        features (Community-only, shipped independently) that predate this
        contract. A superset is safe — nothing constructs these VOs
        positionally (checked: only keyword construction across both repos),
        so field ORDER doesn't gate any calling convention either.
        """
        stub_fields = set(_annotated_field_names(_find_classdef(_parse_stub(), name)))
        real_cls = getattr(sc, name, None)
        assert real_cls is not None, f"safent_composio does not export {name!r}"
        real_fields = {f.name for f in dataclasses.fields(real_cls)}
        missing = stub_fields - real_fields
        assert not missing, f"{name} dropped contract field(s): {missing}"


class TestComposioApiErrorMatchesContract:
    def test_exposes_status_code_and_detail(self) -> None:
        stub_fields = set(_annotated_field_names(_find_classdef(_parse_stub(), "ComposioApiError")))
        exc = sc.ComposioApiError(404, "boom")
        for field_name in stub_fields:
            assert hasattr(exc, field_name), (
                f"ComposioApiError dropped contract field {field_name!r}"
            )
        assert isinstance(exc.status_code, int)
        assert isinstance(exc.detail, str)


class TestComposioClientMatchesContract:
    def test_exposes_every_contract_method(self) -> None:
        client_node = _find_classdef(_parse_stub(), "ComposioClient")
        stub_method_names = {
            stmt.name
            for stmt in client_node.body
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = stub_method_names - set(vars(sc.ComposioClient))
        assert not missing, f"ComposioClient dropped contract method(s): {missing}"

    @pytest.mark.parametrize("method_name", _CLIENT_METHOD_NAMES)
    def test_method_accepts_every_contract_parameter(self, method_name: str) -> None:
        client_node = _find_classdef(_parse_stub(), "ComposioClient")
        stub_buckets = _stub_param_buckets(_find_method(client_node, method_name))

        real_method = getattr(sc.ComposioClient, method_name)
        real_buckets = _real_param_buckets(inspect.signature(real_method))

        dropped = _calling_conventions_dropped(stub_buckets, real_buckets)
        assert not dropped, (
            f"ComposioClient.{method_name} dropped or moved contract "
            f"parameter(s) out of their promised calling convention: {dropped}"
        )
