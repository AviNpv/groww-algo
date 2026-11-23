# Standalone Agent Chat site

This directory is a **separate static website** for the live agent debate. It is
not part of the main dashboard. It contains only HTML/CSS/JS and talks to the
existing backend API over HTTPS.

## Files

- `index.html` — the real streaming chat.
- `agent-demo.html` — the fully scripted, joke-only demo (never calls a model).

## Backend API

By default the page calls `https://groww-algo.onrender.com`. If the page is
served from that host, it uses `location.origin` automatically.

Override the backend with a query parameter:

```
https://your-chat-site.onrender.com/?api=https://your-backend.onrender.com
```

The backend only needs these routes:

- `GET /api/intelligence/status`
- `POST /api/intelligence/chat/start`
- `GET /api/intelligence/chat/{session_id}/stream` (SSE)
- `GET /api/scan` (optional, for symbol chips)

No broker credentials, admin token or model key is used by this website.

## Deploy to Render

The root `render.yaml` already declares a second static service:

```yaml
- type: web
  name: groww-agent-chat
  env: static
  plan: free
  buildCommand: echo "static agent chat"
  staticPublishPath: ./agent-chat-site
```

If your Render blueprint is synced, pushing to `main` creates/updates the
service. Otherwise create a new **Static Site** in Render with:

- Repository: this repo
- Branch: `main`
- Root directory: `agent-chat-site`
- Build command: leave blank (or `echo "static agent chat"`)
- Publish directory: `.` (or `agent-chat-site` if root is repo root)

The resulting URL will be something like
`https://groww-agent-chat.onrender.com`.

## Same-domain copy

For immediate access without creating a new Render service, the same page is
also copied to:

```
frontend/public/agent-chat.html
```

After the frontend build, that file is served at
`https://groww-algo.onrender.com/agent-chat.html`.

## Safety

- AI mode must be enabled on the backend, otherwise start returns HTTP 409.
- One live run at a time, 20-second cooldown.
- Agents are advisory only. Nothing on this site can place an order or change
  strategy config.
- Rate-limited agents are shown as errors and the deterministic decision fails
  closed.
