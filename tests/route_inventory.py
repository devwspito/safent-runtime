"""Walk real FastAPI routes, including lazy includes in newer releases."""

from collections.abc import Iterator, Sequence
from typing import Any


def route_inventory(routes: Sequence[Any], prefix: str = "") -> Iterator[tuple[Any, str]]:
    for route in routes:
        included = getattr(route, "original_router", None)
        context = getattr(route, "include_context", None)
        if included is not None and context is not None:
            yield from route_inventory(included.routes, prefix + context.prefix)
        elif hasattr(route, "path"):
            yield route, prefix + route.path
