<!-- bmad:context -->
<!-- Verified 2026-10-15 against 0011868. Managed by bmad-project-context; edits inside this block are replaced on refresh. Keep anything you want preserved outside the markers. -->

## onshape-mcp-secure

Hardened remote MCP server that gives ClickUp Brain / Super Agents read access to Onshape (Pikes GmbH). Python 3.12, Starlette, MCP SDK 1.x. Live on Fly.io: app `pikes-onshape-mcp`, region `fra`, one machine, volume `onshape_mcp_data` at `/data`. Architecture rules: `_bmad-output/planning-artifacts/architecture/architecture-onshape-mcp-secure-2026-10-15/ARCHITECTURE-SPINE.md`. User docs (German): `docs/DOKUMENTATION.md`; setup details: `README.md`.

## Policy

- Talk to Eric in German, short and crisp; code, comments and commit messages in English.
- Never print secrets (Onshape keys, bearer/admin tokens, webhook secret, link tokens) in chat, logs, docs or commits.
- Never edit `src/onshape_mcp/` (vendored upstream hedless/onshape-mcp, MIT, a9c2a5e); change behaviour in `src/onshape_secure/`.
- Onshape API budget is 2,500 calls/year for the whole company: mapping table first, cache immutable data forever, batch endpoints, never explore. Server limits in `fly.toml` (500/d, 600/w, 1000/m, 2400/y) change only with Eric's OK.
- Never enable or widen write access (`WRITE_MODE=off`) without Eric's explicit OK.
- New upstream tools stay blocked until classified in `policy.py` `TOOL_CLASSES`.
- Every security-relevant change (auth, policy guard, sanitising, budget, config defaults) gets tests.
- `/admin/*` accepts only `ADMIN_TOKEN` (label `admin`); client tokens get 403. `/admin/sync` max. once per 10 min.
- CSV exports pass every cell through `_csv_safe`.
- Non-trivial work follows BMAD v6.12.4 (`_bmad/`, skills in `.claude/skills/bmad-*`, need `uv` on PATH, docs https://docs.bmad-method.org/): bug fix or small change → `bmad-spec` then `bmad-build`; larger feature → `bmad-brainstorming` or `bmad-product-brief` → `bmad-prd` → `bmad-architecture` → `bmad-create-epics-and-stories` → `bmad-sprint-planning`; after implementing → `bmad-code-review`; plan changes → `bmad-correct-course`; after an epic → `bmad-retrospective`; unsure → `bmad-help`. Skip BMAD only for trivial mechanical edits or pure ops (deploy, logs, secrets).
- BMAD artefacts go to `_bmad-output/` and are committed; project knowledge lives in `docs/`.

## Where things are

- Before changing `src/onshape_secure/`: follow the spine (AD-1..AD-14). Where code deviates, the spine lists it as H-n; new code follows the spine, not the deviation.
- `app.py` – Starlette app, MCP transports, metered upstream client, admin endpoints, routes
- `policy.py` – `TOOL_CLASSES` (default deny), `sanitize_arguments`, `check_onshape_request` (HTTP write guard)
- `auth.py` – bearer middleware; `config.py` – env settings, fail closed
- `quota.py` – `OnshapeBudget` (reserve/refund), rate limiter
- `extra_tools.py` – own tools (part lookup, screenshots, exports, where-used, mapping table, sync)
- `mapping.py` – SQLite mapping table `/data/mappings.db`; `webhooks.py` – Onshape webhook receiver; `cache.py`/`images.py` – file cache and capability links
- Tests: `tests/` (ours; e2e uses a fake Onshape via httpx `MockTransport`), `upstream_tests/` (upstream)

## Running and verifying

- Setup: `python -m venv .venv`, activate (Windows `.venv\Scripts\activate`), `pip install --require-hashes --no-deps -r requirements.txt && pip install pytest pytest-asyncio`.
- On Windows: create the venv with Python 3.12 (`uv venv --python 3.12 .venv`; system Python is 3.13) and also install `pywin32==312 win32-setctime==1.2.0` — `requirements.txt` is hashed for the Linux image, and without them every test module fails to import.
- `python -m pytest -q tests` must stay green; upstream: `PYTHONPATH=src python -m pytest -q upstream_tests`.
- Deploy (after `fly auth login`) only with `fly deploy -a pikes-onshape-mcp --ha=false` (exactly one machine), then `curl https://pikes-onshape-mcp.fly.dev/healthz`. New tools appear in ClickUp only after refreshing the MCP connection.
- `fly logs -a pikes-onshape-mcp` shows one `AUDIT` line per tool call; secrets via `fly secrets list` (names only).
- `GET /admin/webhook` – webhook + table status (0 Onshape calls); `POST /admin/webhook` – register webhooks (once).
- `POST /admin/sync` – pull new releases; `?full=1` re-enumerates all latest revisions to refill latest pointers (~1 call per 100 revisions); `?document=<name>&maxParts=50` also links parts → drawings (~1 Onshape call per part).

<!-- /bmad:context -->
