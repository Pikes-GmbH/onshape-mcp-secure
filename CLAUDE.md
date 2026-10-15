# CLAUDE.md – onshape-mcp-secure

Hardened remote MCP server that gives ClickUp Brain / Super Agents access to Onshape (Pikes GmbH).
Live on Fly.io: app `pikes-onshape-mcp`, region `fra`, one machine (shared-cpu-1x, 512 MB, auto-stop), volume `onshape_mcp_data` mounted at `/data`.
User docs: `docs/DOKUMENTATION.md` (German). Setup/details: `README.md`.

## Communication
- Talk to Eric in German, short and crisp. Code, comments and commit messages in English.
- Never print secrets (Onshape keys, bearer/admin tokens, webhook secret) in chat, logs, docs or commits.

## Layout
- `src/onshape_mcp/` – upstream hedless/onshape-mcp (MIT, commit a9c2a5e), vendored **unmodified**. Never edit; change behaviour in the wrapper.
- `src/onshape_secure/` – our code:
  - `app.py` – Starlette app, MCP (Streamable HTTP `/mcp` + legacy SSE), metering wrapper around the upstream client, admin endpoints, routes.
  - `policy.py` – `TOOL_CLASSES` (default deny), `sanitize_arguments`, `check_onshape_request` (HTTP-level write guard).
  - `auth.py` – bearer middleware (constant-time compare). `config.py` – env settings, fail closed.
  - `quota.py` – `OnshapeBudget` (day/week/month/year, refund on 4xx/5xx), rate limiter.
  - `extra_tools.py` – part-number lookup, screenshots, STEP/drawing export, where-used, mapping table tools, batch sync.
  - `mapping.py` – SQLite mapping table (`/data/mappings.db`). `webhooks.py` – Onshape webhook receiver. `cache.py`/`images.py` – 24 h file cache and capability links.
- `tests/` – our tests (`test_units.py`, `test_e2e.py` with a fake Onshape via httpx MockTransport). `upstream_tests/` – upstream tests.
- `_bmad/`, `.claude/skills/bmad-*` – BMAD Method v6.12.4 (German). Planning output goes to `_bmad-output/` (committed).

## Hard rules
- **Onshape API budget is tiny: 2,500 calls/year for the whole company.** Every feature must minimise Onshape calls: cache immutable data (versions/revisions) forever, use the mapping table first, batch endpoints, never "explore".
  Server limits: 500/day, 600/week, 1000/month, 2400/year (set in `fly.toml`, Eric decides changes).
- Write access to Onshape is **OFF** (`WRITE_MODE=off`). Don't enable or widen write paths without Eric's explicit OK.
- New upstream tools stay blocked until classified in `policy.py`.
- Every security-relevant change gets tests (auth, policy guard, sanitising, budget).
- `/admin/*` only accepts `ADMIN_TOKEN` (label `admin`); client tokens (ClickUp) get 403. `/admin/sync` max. once per 10 min.
- CSV exports neutralise formula cells (`_csv_safe`).

## Commands
```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install --require-hashes --no-deps -r requirements.txt pytest pytest-asyncio
python -m pytest -q tests                            # our tests (must stay green)
PYTHONPATH=src python -m pytest -q upstream_tests    # upstream tests
```

## Deploy (Fly.io)
```bash
fly auth login
fly deploy -a pikes-onshape-mcp --ha=false           # builds remotely from Dockerfile
fly logs -a pikes-onshape-mcp                        # AUDIT lines per tool call
fly secrets list -a pikes-onshape-mcp                # ONSHAPE_ACCESS_KEY, ONSHAPE_SECRET_KEY, MCP_AUTH_TOKENS, ADMIN_TOKEN
```
After deploy: `curl https://pikes-onshape-mcp.fly.dev/healthz`. New tools appear in ClickUp only after refreshing the MCP connection.

## Admin endpoints (need ADMIN_TOKEN)
- `GET /admin/webhook` – webhook + table status (0 Onshape calls). `POST /admin/webhook` – register webhooks (once).
- `POST /admin/sync` – pull new releases into the table; `?document=<name>&maxParts=50` also links parts → drawings (~1 call per part).
