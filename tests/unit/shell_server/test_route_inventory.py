"""Security sweeps must not silently omit nested included routers."""

from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.routing import APIRoute, APIWebSocketRoute

from tests.route_inventory import route_inventory


def test_inventory_preserves_nested_prefixes_and_websockets():
    app = FastAPI()
    parent = APIRouter()
    child = APIRouter()

    @child.get("/items")
    def items():
        return []

    @child.websocket("/live")
    async def live(websocket: WebSocket):
        await websocket.close()

    parent.include_router(child, prefix="/nested")
    app.include_router(parent, prefix="/api/v1")
    found = {
        (type(route), path)
        for route, path in route_inventory(app.routes)
        if isinstance(route, (APIRoute, APIWebSocketRoute))
    }
    assert found == {
        (APIRoute, "/api/v1/nested/items"),
        (APIWebSocketRoute, "/api/v1/nested/live"),
    }
