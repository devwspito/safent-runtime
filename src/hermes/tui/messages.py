"""hermes.tui.messages — Textual messages that carry daemon D-Bus signals.

Signal callbacks fire on the same loop as the app, so they `post_message` one
of these; screens react by handling the typed message. Keeps signal plumbing
out of the widgets.
"""

from __future__ import annotations

from textual.message import Message


class ChatDelta(Message):
    """A streamed chat token (org.hermes.Runtime1.ChatDelta)."""

    def __init__(self, conversation_id: str, seq: int, text: str) -> None:
        self.conversation_id = conversation_id
        self.seq = seq
        self.text = text
        super().__init__()


class ChatStreamEnd(Message):
    """End of a chat stream (org.hermes.Runtime1.ChatStreamEnd)."""

    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        super().__init__()


class ApprovalRequested(Message):
    """A HITL gate awaits the owner's decision (ApprovalRequested)."""

    def __init__(self, payload_json: str) -> None:
        self.payload_json = payload_json
        super().__init__()


class LivenessChanged(Message):
    """Agent up/down + has-model (AgentLivenessChanged)."""

    def __init__(self, alive: bool, has_model: bool) -> None:
        self.alive = alive
        self.has_model = has_model
        super().__init__()


class TaskStatusChanged(Message):
    """A task changed state (TaskStatusChanged / TaskEnqueued)."""

    def __init__(self, task_id: str, status: str) -> None:
        self.task_id = task_id
        self.status = status
        super().__init__()


class ScanCompleted(Message):
    """A security scan finished (ScanCompleted)."""

    def __init__(self, scan_id: str, verdict: str) -> None:
        self.scan_id = scan_id
        self.verdict = verdict
        super().__init__()


class InstallReviewRequested(Message):
    """An install needs the owner's review at the Security Center gate."""

    def __init__(self, scan_id: str, scan_data_json: str) -> None:
        self.scan_id = scan_id
        self.scan_data_json = scan_data_json
        super().__init__()
