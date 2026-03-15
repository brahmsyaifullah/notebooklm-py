# Railway Deployment

**Status:** Active
**Last Updated:** 2026-03-16

This project includes a Railway-ready runtime for health checks and managed uptime.

## Included Files

- `railway.toml` - Railway build/deploy configuration
- `Procfile` - Fallback process definition (`web` process)
- `railway_server.py` - Minimal HTTP runtime (`/`, `/health`, `/ready`)

## One-Time Railway Setup

1. Create a new Railway service from this repository.
2. Ensure the service type is **Web Service**.
3. Set required environment variables:
   - `NOTEBOOKLM_AUTH_JSON` - Playwright storage JSON (recommended)
   - `NOTEBOOKLM_HOME=/data/notebooklm` (recommended if using volume)
4. (Optional) Mount a persistent volume and point `NOTEBOOKLM_HOME` to it.

Railway sets `PORT` automatically. The runtime binds to `0.0.0.0:$PORT`.

## Health Endpoints

- `GET /`
- `GET /health`
- `GET /ready`

Each endpoint returns JSON with runtime status and auth source (`env`, `file`, or `none`).

## Running NotebookLM Operations

The Railway runtime only handles health checks. Run NotebookLM commands via:

- Railway Shell
- Railway cron jobs
- Separate worker service command

Examples:

```bash
python3 -m pip install . && python3 -m notebooklm.notebooklm_cli auth check --test
python3 -m pip install . && python3 -m notebooklm.notebooklm_cli list --json
```

## Authentication Recommendation

For cloud deployments, prefer `NOTEBOOKLM_AUTH_JSON` instead of browser login.

You can extract it locally from:

```bash
cat ~/.notebooklm/storage_state.json
```

Then store that value in Railway service variables.

