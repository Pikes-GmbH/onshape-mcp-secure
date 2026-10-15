---
review-of: ARCHITECTURE-SPINE.md
lens: versions-and-reality-check
date: '2026-10-15'
verdict: 'Stack matches the repo and has no known CVEs, but three facts are stale or undocumented: MCP SDK 1.x is now the maintenance line, the base image runs on Debian bookworm LTS, and legacy SSE is formally deprecated.'
---

# Review: versions and reality check of the Architecture Spine

**Lens:** Is each committed decision backed by research or a reality check, rather than asserted from training data? Are the versions current, still maintained, and free of known advisories?

**Verdict:** The Stack table matches `requirements.txt`, `Dockerfile` and `fly.toml` exactly. OSV reports no known vulnerabilities for any of the 30 pins. The Onshape events and the endpoint all exist. The gap is documentation: the spine lists no sources, so none of its version or lifecycle claims can be traced. Three statements are stale or incomplete (F1–F3).

## 1. Reality check: spine vs. repo

| Spine (Stack) | Repo | Match |
| --- | --- | --- |
| Python 3.12, `python:3.12-slim-bookworm`, pinned by digest | `Dockerfile`: `python:3.12-slim-bookworm@sha256:34386ef0…` (comment 2026-10-06) | yes |
| mcp 1.30.0 | `requirements.txt` `mcp==1.30.0` | yes |
| Starlette 1.8.0 | `starlette==1.8.0` | yes |
| uvicorn 0.54.0 | `uvicorn==0.54.0` | yes |
| httpx 0.28.1 | `httpx==0.28.1` | yes |
| loguru 0.7.3 | `loguru==0.7.3` | yes |
| `--require-hashes` | `Dockerfile` `pip install --require-hashes --no-deps` | yes |
| Fly `pikes-onshape-mcp`, `fra`, shared-cpu-1x/512 MB, Auto-Stop, Volume `/data` | `fly.toml` (`auto_stop_machines="stop"`, `min_machines_running=0`, mount `onshape_mcp_data`) | yes |
| Limits 500/600/1000/2400 (AD-4) | `fly.toml [env]` | yes |
| `WRITE_MODE=off` (AD-5) | `fly.toml [env]` | yes |
| Streamable HTTP `/mcp` + legacy SSE | `app.py`: `StreamableHTTPSessionManager(stateless=True, json_response=True)`, `SseServerTransport` behind `ENABLE_LEGACY_SSE` (default **True**, not set in `fly.toml`, so **active** in production) | yes |

Missing from the Stack table but security-relevant: `sse-starlette==3.5.0`, `pydantic==2.14.1`, `pyjwt==2.15.1`, `cryptography==50.0.2`, `python-multipart==0.0.32`, `h11==0.16.0`.

## 2. Versions vs. current state (as of 2026-10-15)

| Component | Pinned | Current | Advisories (OSV/PyPI) | Assessment |
| --- | --- | --- | --- | --- |
| mcp (Python SDK) | 1.30.0 (2026-09-07) | **2.3.0** (2026-10-02); 2.0.0 GA on 2026-07-28 | none | 1.30.0 is the newest 1.x, but **1.x has been the maintenance line since 2026-07-28** (security and critical fixes only). v2 renames APIs (`FastMCP`→`MCPServer`, `mcp.types`→`mcp-types`, `McpError`→`MCPError`) and targets spec 2026-07-28. Not mentioned in the spine. |
| Starlette | 1.8.0 (2026-10-13) | 1.8.0 | none | current (released 2 days ago, so little field use yet) |
| uvicorn | 0.54.0 (2026-09-25) | 0.54.0 | none | current |
| httpx | 0.28.1 (2024-12-06) | 0.28.1 | none | current, but no release in almost 2 years. Fits `mcp` (`httpx<1.0.0,>=0.27.1`). Fine; noted only. |
| Python | 3.12 (digest from 2026-10-06) | 3.14 stable; 3.12 **security-only** since 2025-04, EOL 2028-10 | n/a | maintained, but no longer bugfixed; patch level not pinned in the spine |
| Debian base | bookworm | trixie (Debian 13) is stable | n/a | **Bookworm regular support ended on 2026-07-12**; LTS (reduced package set) runs until 2028-06-30. Debian recommends moving to trixie. |
| Transitive (all 30 pins) | — | — | OSV querybatch: **0 hits** | ok |

Dependency fit: `mcp 1.30.0` requires `starlette>=0.27`, `uvicorn>=0.31.1`, `sse-starlette>=1.6.1` and `pydantic>=2.11,<3`. All pins satisfy these.

## 3. Onshape API reality check

Source: Onshape OpenAPI (`https://cad.onshape.com/api/openapi?includedTags=Webhook`, base `/api/v17`), callbacks of `createWebhook`.

| Used in code | Status |
| --- | --- |
| `onshape.revision.created` (`webhooks.py` `RELEASE_EVENTS`) | exists ("Occurs when a revision is created.") |
| `onshape.workflow.transition` | exists ("…revision or release package transitions through workflow states") |
| `onshape.model.lifecycle.metadata` (`METADATA_EVENT`) | exists ("…part or element metadata is modified") |
| `POST /api/webhooks`, `GET /api/webhooks/{id}` (`app.py`) | exists as `POST /webhooks`. The code calls it **without a version prefix**; the public docs use `/api/v6/webhooks` and the current OpenAPI uses `/api/v17`. This works in practice, but which version Onshape resolves an unversioned path to is not documented. |
| `isTransient: False` | correct and important: the Onshape docs say webhooks are otherwise cleaned up after inactivity |

Not mentioned in the spine but relevant to AD-7: Onshape Company/Enterprise accounts can **sign** webhook payloads (HMAC-SHA256 over timestamp + payload, headers `X-onshape-webhook-signature-primary/secondary`, key rotation) and can set Basic Auth. AD-7 names "the secret in the path" as the only credential, which was not compared against this option.

## 4. MCP transport: legacy SSE

- The MCP spec revision **2026-07-28** reclassified HTTP+SSE (deprecated since 2025-03-26) as **Deprecated** under the formal lifecycle policy (SEP-2596). Secondary sources say there is a 12-month minimum before removal, so removal in the spec is possible from about 2027-07 at the earliest (not confirmed against the registry itself).
- The same revision **removes protocol-level sessions** (`Mcp-Session-Id`), the GET stream endpoint and the `initialize` handshake (replaced by `server/discover` plus `_meta` per request). The `/mcp` endpoint already runs with `stateless=True` and `json_response=True`, which suits this well. SDK 1.x does **not** speak the 2026-07-28 revision. v2 does, and stays backwards compatible.
- Large providers (e.g. Cloudflare) no longer serve HTTP+SSE and map old `/sse` URLs to Streamable HTTP.
- The spine only says "Legacy-SSE abschalten — hängt davon ab, ob ClickUp noch SSE nutzt". That is correct, but it leaves out the spec status and the fact that SSE is **on by default** in production.

## Findings

### F1 — MCP SDK 1.x is the maintenance line; no decision on v2 [Medium]
- **Problem:** The Stack table presents `mcp 1.30.0` as current. Since 2026-07-28, 2.x is the stable line (now 2.3.0) and 1.x only gets security and critical fixes. New spec features (2026-07-28: stateless without handshake, `server/discover`) come only in v2. If ClickUp moves to the new revision, the 1.x server can only answer through the backwards-compatible path.
- **Fix:** Add a version-policy row to the Stack table: "mcp 1.x (maintenance line, `<2` cap), migration to 2.x as its own `bmad-spec`". Add a Deferred entry "MCP SDK v2 migration" with a trigger (security fixes in 1.x stop, ClickUp requires 2026-07-28, or upstream `hedless/onshape-mcp` moves to v2). Check whether upstream (AD-1) imports `mcp.server.fastmcp`; if so, the migration depends on an upstream update.

### F2 — Base image is on Debian bookworm LTS; Python 3.12 is security-only [Medium]
- **Problem:** Bookworm regular support ended on 2026-07-12. LTS covers only a reduced package set until 2028-06. Python 3.12 has been security-only since 2025-04. The spine records `3.12-slim-bookworm` as fixed with no lifecycle note. The digest pin also freezes OS patches unless someone updates it manually; no rhythm is defined for that.
- **Fix:** Switch to `python:3.12-slim-trixie` (or `3.13-slim-trixie` after a test run) and note the EOL dates in the Stack table (Python 3.12: 2028-10, bookworm LTS: 2028-06). Define a digest refresh rhythm (e.g. monthly or per deploy) under Consistency Conventions / Deploy.

### F3 — Legacy SSE: spec status not documented, enabled by default in production [Medium]
- **Problem:** HTTP+SSE is formally Deprecated in spec 2026-07-28. `ENABLE_LEGACY_SSE` defaults to `True` and is not set in `fly.toml`, so a second, older, publicly reachable transport is active. The spine's Deferred row does not give the spec status or a check for whether ClickUp actually uses it.
- **Fix:** Update the Deferred row: "HTTP+SSE deprecated (MCP 2026-07-28, SEP-2596). Check: count AUDIT/access logs for SSE connections over 30 days; if there are none, set `ENABLE_LEGACY_SSE=false` in `fly.toml`." Long term, change the `config.py` default to `False` (fail-closed, AD-10).

### F4 — No source evidence in the spine [Low]
- **Problem:** `sources:` lists only repo files. None of the version, lifecycle or Onshape API claims has a link or a "checked on" date. The claims are correct for the repo, but the spine does not show that they were checked against the outside world.
- **Fix:** Add a "Verified (2026-10-15)" column or a short source block to the Stack table: PyPI/OSV, the Onshape OpenAPI `createWebhook` callbacks, the MCP changelog 2026-07-28, and Debian News 2026-07-12.

### F5 — Unversioned Onshape endpoint; webhook signatures not considered [Low]
- **Problem:** `app.py` calls `/api/webhooks` without a version, while the docs use `/api/v6` and the OpenAPI uses `/api/v17`. AD-7 commits to the path secret as the only credential, although Onshape offers HMAC signatures for company accounts.
- **Fix:** Pin the version explicitly (e.g. `/api/v6/webhooks`, test against the fake Onshape) and note in AD-7: "HMAC signature (`X-onshape-webhook-signature-*`) checked: [used / not available in our plan]". This is an additional check, not a replacement for "webhooks only invalidate".

## What is confirmed and needs no action
- Starlette 1.8.0, uvicorn 0.54.0, httpx 0.28.1 and loguru 0.7.3 are the latest versions and have no known advisories.
- All 30 hash-pinned packages show 0 hits in OSV (querybatch, 2026-10-15).
- `onshape.revision.created`, `onshape.workflow.transition`, `onshape.model.lifecycle.metadata` and `createWebhook` exist in Onshape's current OpenAPI.
- `/mcp` is stateless with JSON responses, which already fits the session-free MCP revision 2026-07-28.

## Sources
- PyPI JSON: https://pypi.org/pypi/mcp/json, https://pypi.org/pypi/starlette/1.8.0/json, https://pypi.org/pypi/uvicorn/0.54.0/json, https://pypi.org/pypi/httpx/0.28.1/json
- OSV: https://api.osv.dev/v1/querybatch (all pins from `requirements.txt`)
- MCP SDK v2 / migration: https://py.sdk.modelcontextprotocol.io/migration/
- MCP spec 2026-07-28: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http, https://modelcontextprotocol.net/specification/2026-07-28/changelog
- Cloudflare MCP 2026-07-28: https://developers.cloudflare.com/changelog/post/2026-07-28-cloudflare-mcp-servers-mcp-2026-07-28/
- Debian bookworm → LTS: https://www.debian.org/News/2026/20260712.en.html
- Python EOL: https://endoflife.date/python
- Onshape webhooks: https://onshape-public.github.io/docs/app-dev/webhook/, https://cad.onshape.com/api/openapi?includedTags=Webhook, https://cad.onshape.com/help/Content/Plans/enterprise_settings_webhooks.htm
