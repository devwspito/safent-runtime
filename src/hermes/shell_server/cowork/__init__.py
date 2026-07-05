"""Safent Cowork — web UI support routers for the shell-server.

Three routers live here, all mounted under /api/v1 by main.py:
  - chat_stream   : WS /api/v1/chat/stream/{task_id}  (AF_UNIX bridge)
  - workspace_api : GET /api/v1/workspace/files + /file/{name}
  - approvals_api : GET/POST /api/v1/approvals/*  (HITL bridge)
"""
