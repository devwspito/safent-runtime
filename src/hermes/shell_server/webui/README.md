# Lumen Cowork — Web UI

Static web app served by the Hermes shell-server (FastAPI).

## How it's served

| Path | What |
|------|------|
| `/` | Serves `index.html` |
| `/webui/*` | Static assets (`style.css`, `js/`, `assets/`) |
| `/api/v1/*` | FastAPI REST endpoints (same origin) |
| `ws://<host>/api/v1/chat/stream/{task_id}` | WebSocket task stream |

FastAPI mounts this directory as `StaticFiles` at `/webui` and redirects `/` → `index.html`.

## File map

```
webui/
├── index.html          Entry point — full 3-pane shell HTML
├── style.css           Sereno design system + all component styles
├── assets/
│   └── InterVariable.woff2   Inter variable font (optional; system stack fallback)
└── js/
    ├── app.js          Entry module — orchestrates all sub-modules
    ├── api.js          Fetch wrapper for /api/v1/* endpoints
    ├── stream.js       WebSocket client for task streaming
    ├── chat.js         Chat renderer + streaming controller
    ├── composer.js     Composer bar (input, send, mode picker)
    ├── approvals.js    HITL approval card polling + rendering
    ├── context-panel.js Right panel: files, skills, connectors
    ├── recents.js      Sidebar recents list
    ├── markdown.js     Safe Markdown → HTML renderer (no deps)
    ├── icons.js        Inline SVG icon library
    ├── shell.js        Layout wiring: sidebar, toasts, keyboard shortcuts
    └── theme.js        Dark/light theme management
```

## Opening locally

Open `http://localhost:7517/` after starting the shell-server:

```bash
cd src && python -m hermes.shell_server.main
```

Or dev mode (assumes you have the static-file mount in main.py):

```bash
uvicorn hermes.shell_server.main:create_app --factory --port 7517 --reload
```

The Inter font is loaded from `/webui/assets/InterVariable.woff2` — if the file
is absent the UI falls back to the system sans-serif stack and looks correct.
