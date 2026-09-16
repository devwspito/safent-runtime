"""Exact owner-only task status for reconnect; never infer success from an error."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes.shell_server.cowork.task_dashboard_api import create_task_dashboard_router
from hermes.tasks.control_plane.domain.ports import AgentUnavailable, TaskStatusView, UnknownTask
from hermes.tasks.domain.ports import TaskStatus

pytestmark = pytest.mark.unit
OWNER = "synthetic-owner-session"


@pytest.fixture
def status_api():
    task_id = uuid4()
    view = TaskStatusView(
        task_id=task_id,
        status="pending",
        attempts=1,
        enqueued_by="PRIVATE owner",
        stream_path="PRIVATE path",
        error="PRIVATE provider error",
    )
    app = FastAPI()
    app.state.shell_webui_token = OWNER
    app.state.control_plane = AsyncMock()
    app.state.control_plane.get_task_status.return_value = view
    app.include_router(create_task_dashboard_router())
    return TestClient(app), app.state.control_plane, view


@pytest.mark.parametrize("status", [status.value for status in TaskStatus])
def test_exact_status_is_minimal_and_never_calls_recent_or_mutators(status_api, status):
    client, control, view = status_api
    control.get_task_status.return_value = replace(view, status=status)
    response = client.get(
        f"/api/v1/tasks/{view.task_id}/status", headers={"Authorization": f"Bearer {OWNER}"}
    )
    assert response.status_code == 200
    assert response.json() == {"task_id": str(view.task_id), "status": status, "attempts": 1}
    control.get_task_status.assert_awaited_once_with(task_id=view.task_id)
    assert len(control.mock_calls) == 1
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("authorization", [None, "Bearer internal-daemon", "Bearer wrong-owner"])
def test_only_the_owner_can_query_status(status_api, authorization):
    client, control, view = status_api
    response = client.get(
        f"/api/v1/tasks/{view.task_id}/status",
        headers={"Authorization": authorization} if authorization else {},
    )
    assert response.status_code == 403
    control.get_task_status.assert_not_awaited()


@pytest.mark.parametrize(
    "failure,expected",
    [
        (UnknownTask("PRIVATE"), 404),
        (AgentUnavailable("PRIVATE"), 503),
        (TimeoutError("PRIVATE"), 503),
        (RuntimeError("PRIVATE"), 503),
    ],
)
def test_unavailable_or_unknown_is_not_terminal_evidence(status_api, failure, expected):
    client, control, view = status_api
    control.get_task_status.side_effect = failure
    response = client.get(
        f"/api/v1/tasks/{view.task_id}/status", headers={"Authorization": f"Bearer {OWNER}"}
    )
    assert response.status_code == expected
    assert "PRIVATE" not in response.text
    assert "status" not in response.json()


@pytest.mark.parametrize(
    "changes", [{"task_id": uuid4()}, {"status": "unknown"}, {"attempts": True}, {"attempts": -1}]
)
def test_invalid_daemon_metadata_fails_closed(status_api, changes):
    client, control, view = status_api
    control.get_task_status.return_value = replace(view, **changes)
    response = client.get(
        f"/api/v1/tasks/{view.task_id}/status", headers={"Authorization": f"Bearer {OWNER}"}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "task_status_unavailable"}}


def test_invalid_id_never_reaches_control_plane(status_api):
    client, control, _ = status_api
    response = client.get(
        "/api/v1/tasks/not-a-uuid/status", headers={"Authorization": f"Bearer {OWNER}"}
    )
    assert response.status_code == 422
    control.get_task_status.assert_not_awaited()
