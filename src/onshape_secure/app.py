"""Hardened remote (HTTP) MCP server around the upstream onshape-mcp tool set."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import os
import re
import sys
import time
from typing import Any

import anyio
import httpx
from loguru import logger
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ImageContent, TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import __version__
from .auth import BearerAuthMiddleware, current_client
from .config import ADMIN_LABEL, Settings, load_settings
from .policy import (
    CREATE_DOC,
    DELETE,
    FEATURESCRIPT,
    META,
    READ,
    SYSTEM,
    WRITE,
    EXTRA_TOOL_NAMES,
    DocumentAccess,
    PolicyDenied,
    check_onshape_request,
    is_tool_enabled,
    sanitize_arguments,
    sanitize_schema,
    tool_class,
)
from .extra_tools import TOOLS as EXTRA_TOOLS
from .cache import DownloadLinks, FileCache, KVCache
from .extra_tools import ExtraTools
from .images import ImageStore
from .mapping import MappingStore
from .webhooks import METADATA_EVENT, RELEASE_EVENTS
from .webhooks import make_receiver
from .quota import OnshapeBudget, QuotaExceeded, RateLimiter

ADMIN_SYNC_COOLDOWN = 600  # seconds between two /admin/sync runs

TOOL_TIMEOUT_SECONDS = 180
POLICY_TOOL = "get_server_policy"
SEMAPHORE_WAIT_SECONDS = 30
MAX_SSE_SESSIONS = 10
# per tool call: {"cls": tool class, "doc": gated documentId, "requests": int, "blocked": int}
_call_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("call_ctx", default=None)


def _configure_logging(level: str) -> None:
    logger.remove()
    # diagnose/backtrace off: loguru would otherwise print local variables of every
    # stack frame on exceptions - including the Onshape Authorization header.
    logger.add(
        sys.stderr,
        level=level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}",
        backtrace=False,
        diagnose=False,
    )


def _audit(**fields: Any) -> None:
    fields = {"event": "tool_call", **fields}
    logger.info("AUDIT " + json.dumps(fields, ensure_ascii=False, default=str))


DESCRIPTION_HINTS = {
    name: " Not for finding part numbers - use find_part_by_number. Costs Onshape API budget."
    for name in ("search_documents", "list_documents", "find_part_studios", "get_parts", "get_elements",
                 "get_document_summary", "get_document")
}


def _instructions(settings: Settings) -> str:
    lines = [
        "Onshape CAD server (hardened). IDs: documentId/workspaceId/elementId are 24-char hex strings.",
        "API BUDGET IS TINY (about 2,500 Onshape calls per YEAR for the whole company). Every tool call that "
        "reaches Onshape costs budget. Use the fewest calls possible, never browse or explore, and ask the user "
        "instead of guessing.",
        "PART NUMBERS (e.g. PRT-000583, DRW-000635): ALWAYS start with find_part_by_number - it covers released "
        "AND unreleased parts from the server's own index. Never use search_documents, list_documents, "
        "find_part_studios, get_parts or get_elements to hunt for a part number. If find_part_by_number says "
        "not found, you may try search_part_index (free) with the part name; otherwise STOP and ask the user "
        "which document it is in.",
    ]
    if settings.write_mode == "off":
        lines.append("This server is READ-ONLY.")
    elif settings.write_mode == "allowlist":
        rule = "Changes are only allowed in allow-listed documents"
        if settings.write_allowed_name_prefix:
            rule += f" and in documents whose name starts with '{settings.write_allowed_name_prefix}'"
        lines.append(rule + ". To build something new, call create_document first.")
    lines.append("Call get_server_policy to see the current rules and the remaining Onshape API budget.")
    lines.append(
        "With a part number go straight to capture_part_screenshot, export_released_model (STEP), "
        "export_released_drawing (PDF) or find_drawing_for_part - all accept partNumber. show_mapping_table is "
        "free. Screenshot results contain a markdown image line and exports a markdown download link - include "
        "them unchanged in your reply. Every result ends with an [Onshape-API: ...] line: at the end of your "
        "answer, tell the user the TOTAL Onshape API calls your answer cost (sum of those lines) and the "
        "current year usage."
    )
    lines.append(
        "Assemblies: ground a reference part, read face IDs with get_body_details, create mates in "
        "small batches and verify with get_assembly_features / get_assembly_positions."
    )
    return "\n".join(lines)


def _truncate(content: list, max_chars: int) -> list:
    out = []
    for item in content:
        if isinstance(item, TextContent) and len(item.text) > max_chars:
            item = TextContent(
                type="text",
                text=item.text[:max_chars] + f"\n... [truncated, {len(item.text) - max_chars} more characters]",
            )
        out.append(item)
    return out


def _deny(message: str, *extra: TextContent) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=message), *extra], isError=True)


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or load_settings()
    _configure_logging(settings.log_level)

    # Import the upstream tool implementation only after the configuration is validated.
    from onshape_mcp import server as upstream

    _configure_logging(settings.log_level)  # upstream import resets loguru to DEBUG
    upstream.credentials.access_key = settings.onshape_access_key
    upstream.credentials.secret_key = settings.onshape_secret_key
    upstream.credentials.base_url = settings.onshape_base_url
    upstream.client.base_url = settings.onshape_base_url

    budget = OnshapeBudget(
        settings.usage_file, settings.onshape_daily_limit, settings.onshape_monthly_limit,
        settings.onshape_yearly_limit, weekly_limit=settings.onshape_weekly_limit,
    )

    def _usage_line(ctx: dict) -> TextContent:
        """Cost of this tool call in Onshape API calls - the agent passes it on to the user."""
        b = budget.snapshot()

        def part(label, used, limit):
            return f"{label} {used}/{limit}" if limit != "unlimited" else f"{label} {used}"

        return TextContent(type="text", text=(
            f"[Onshape-API: {ctx.get('billed', 0)} Aufruf(e) für diese Anfrage · "
            + " · ".join([
                part("heute", b["today_utc"], b["daily_limit"]),
                part("Woche", b["this_week_utc"], b["weekly_limit"]),
                part("Monat", b["this_month_utc"], b["monthly_limit"]),
                part("Jahr", b["this_year_utc"], b["yearly_limit"]),
            ]) + "]"
        ))

    async def _billed(call):
        """Run one Onshape request; refund the budget unit if Onshape doesn't bill it (error answer)."""
        ctx = _call_ctx.get()
        if ctx is not None:
            ctx["billed"] = ctx.get("billed", 0) + 1
        try:
            return await call()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            if isinstance(exc, httpx.TransportError) or exc.response.status_code >= 400:
                await budget.refund()
                if ctx is not None:
                    ctx["billed"] -= 1
            raise
    limiter = RateLimiter(settings.tool_calls_per_minute)
    semaphore = asyncio.Semaphore(settings.max_concurrent_calls)

    # Every Onshape HTTP request goes through the shared upstream client: guard and meter it.
    def _metered(method_name, method):
        async def wrapper(path, *args, **kwargs):
            ctx = _call_ctx.get()
            try:
                check_onshape_request(method_name, str(path), ctx and ctx["cls"], ctx and ctx["doc"])
            except PolicyDenied:
                if ctx is not None:
                    ctx["blocked"] += 1
                logger.warning(f"blocked {method_name.upper()} outside tool permission (tool class {ctx and ctx['cls']})")
                raise
            await budget.reserve()
            if ctx is not None:
                ctx["requests"] += 1
            return await _billed(lambda: method(path, *args, **kwargs))

        return wrapper

    originals = getattr(upstream, "_secure_original_methods", None)
    if originals is None:  # keep the unwrapped methods so create_app() can run more than once
        originals = {m: getattr(upstream.client, m) for m in ("get", "post", "delete")}
        upstream._secure_original_methods = originals
    for method_name, method in originals.items():
        setattr(upstream.client, method_name, _metered(method_name, method))
    raw_get = originals["get"]

    async def fetch_document_name(document_id: str) -> str:
        await budget.reserve()
        ctx = _call_ctx.get()
        if ctx is not None:
            ctx["requests"] += 1
        data = await _billed(lambda: raw_get(f"/api/v6/documents/{document_id}"))
        return str(data.get("name", ""))

    async def get_bytes(path: str, max_bytes: int) -> bytes:
        """Binary GET (file downloads) through the same guard + budget as every other request."""
        ctx = _call_ctx.get()
        check_onshape_request("get", path, ctx and ctx["cls"], ctx and ctx["doc"])
        await budget.reserve()
        if ctx is not None:
            ctx["requests"] += 1
        client = upstream.client
        client._ensure_client()
        headers = {"Authorization": client._get_auth_header(), "Accept": "application/octet-stream"}
        async def download() -> bytes:
            chunks, size = [], 0
            # httpx drops the Authorization header on cross-origin redirects
            async with client._client.stream(
                "GET", f"{client.base_url}{path}", headers=headers, follow_redirects=True, timeout=120
            ) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise PolicyDenied("Exported file is larger than the configured download limit.")
                    chunks.append(chunk)
            return b"".join(chunks)

        return await _billed(download)

    access = DocumentAccess(settings, fetch_document_name)

    data_dir = os.path.dirname(settings.usage_file) or "."
    images: ImageStore | None = None
    links: DownloadLinks | None = None
    kv: KVCache | None = None
    files: FileCache | None = None
    try:
        kv = KVCache(os.path.join(data_dir, "cache.json"))
        files = FileCache(
            os.path.join(data_dir, "files"), settings.cache_max_mb * 1024 * 1024, settings.file_cache_hours * 3600
        )
    except OSError as exc:
        logger.warning(f"Persistent cache disabled ({exc})")
    if settings.image_links and settings.public_base_url:
        image_dir = settings.image_dir or os.path.join(data_dir, "images")
        try:
            images = ImageStore(image_dir, settings.image_ttl_hours * 3600, settings.public_base_url)
            links = DownloadLinks(
                os.path.join(data_dir, "links"), settings.image_ttl_hours * 3600, settings.public_base_url
            )
        except OSError as exc:
            logger.warning(f"Image/download links disabled, cannot use {data_dir!r}: {exc}")
    mapping: MappingStore | None = None
    try:
        mapping = MappingStore(os.path.join(data_dir, "mappings.db"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Mapping table disabled ({exc})")
    extra = ExtraTools(
        upstream.client, access.check_read, get_bytes=get_bytes, kv=kv, files=files, links=links, mapping=mapping,
        web_base_url=settings.onshape_base_url,
    )

    async def _verify_webhook_if_due() -> None:
        """At most weekly: is Onshape still delivering release events? (1 GET, only when a tool runs)."""
        if mapping is None or not mapping.meta("webhook_id"):
            return
        verified = float(mapping.meta("webhook_verified_at") or 0)
        if time.time() - verified < 7 * 86400:
            return
        wid = mapping.meta("webhook_id")
        try:
            info = await upstream.client.get(f"/api/webhooks/{wid}")
        except Exception as exc:  # noqa: BLE001
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                logger.warning("release webhook vanished - falling back to timed re-checks")
                mapping.set_meta("webhook_id", None)
                mapping.invalidate_all()
            return
        dropped = int(info.get("droppedEventCount") or 0) if isinstance(info, dict) else 0
        if dropped > int(mapping.meta("webhook_dropped") or 0):
            logger.warning(f"Onshape dropped {dropped} webhook events - invalidating mappings")
            mapping.invalidate_all()
        mapping.set_meta("webhook_dropped", str(dropped))
        mapping.set_meta("webhook_verified_at", str(time.time()))

    def _add_image_links(content: list) -> list:
        if images is None:
            return content
        out = []
        for item in content:
            out.append(item)
            if isinstance(item, ImageContent) and item.mimeType == "image/png":
                url = images.save_base64(item.data)
                if url:
                    out.append(
                        TextContent(
                            type="text",
                            text=(
                                f"![Onshape screenshot]({url})\n"
                                f"Image link (valid {settings.image_ttl_hours} h): {url}"
                            ),
                        )
                    )
        return out

    published: dict[str, Tool] = {}

    async def _published_tools() -> dict[str, Tool]:
        if not published:
            for tool in await upstream.list_tools():
                if not is_tool_enabled(tool.name, settings):
                    continue
                published[tool.name] = Tool(
                    name=tool.name,
                    description=(tool.description or "") + DESCRIPTION_HINTS.get(tool.name, ""),
                    inputSchema=sanitize_schema(tool.name, tool.inputSchema),
                )
            for tool in EXTRA_TOOLS:
                if is_tool_enabled(tool.name, settings):
                    published[tool.name] = tool
            published[POLICY_TOOL] = Tool(
                name=POLICY_TOOL,
                description="Show this server's safety rules (where writing is allowed) and the remaining Onshape API budget.",
                inputSchema={"type": "object", "properties": {}},
            )
        return published

    def _policy_text() -> str:
        info = {
            "write_mode": settings.write_mode,
            "writable_name_prefix": settings.write_allowed_name_prefix or None,
            "allow_listed_documents": len(settings.write_allowed_document_ids),
            "read_restricted": settings.read_restricted,
            "delete_feature_allowed": settings.allow_delete and settings.write_mode != "off",
            "featurescript_allowed": settings.allow_featurescript,
            "new_documents_are_private": True,
            "disabled_tools": sorted(settings.disabled_tools),
            "tool_calls_per_minute": settings.tool_calls_per_minute,
            "onshape_api_budget": budget.snapshot(),
        }
        return json.dumps(info, indent=2)

    server: Server = Server("onshape-mcp-secure", version=__version__, instructions=_instructions(settings))

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return list((await _published_tools()).values())

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> Any:
        started = time.monotonic()
        client = current_client.get()
        cls = tool_class(name) if name != POLICY_TOOL else META
        doc_id = (arguments or {}).get("documentId") if isinstance(arguments, dict) else None
        doc_id = doc_id if isinstance(doc_id, str) and len(doc_id) <= 64 else None
        ctx = {"cls": cls, "doc": None, "requests": 0, "blocked": 0, "billed": 0}
        token = _call_ctx.set(ctx)

        def audit(decision: str, **extra: Any) -> None:
            _audit(
                client=client,
                tool=name[:80],
                cls=cls,
                decision=decision,
                document=doc_id,
                onshape_requests=ctx["requests"],
                blocked_requests=ctx["blocked"] or None,
                ms=int((time.monotonic() - started) * 1000),
                **extra,
            )

        try:
            limiter.hit(client)
            if name == POLICY_TOOL:
                audit("allowed")
                return [TextContent(type="text", text=_policy_text()), _usage_line(ctx)]

            tools = await _published_tools()
            if name not in tools:
                raise PolicyDenied(f"Unknown or disabled tool: {name[:80]}")

            args = sanitize_arguments(name, arguments, tools[name].inputSchema, settings)
            if cls in (WRITE, DELETE):
                await access.check_write(args.get("documentId"))
            elif cls != CREATE_DOC:
                await access.check_read(args.get("documentId"))
            if cls == FEATURESCRIPT and not settings.allow_featurescript:
                raise PolicyDenied("FeatureScript evaluation is disabled.")
            await budget.check()
            ctx["doc"] = args.get("documentId")  # the only document a write may touch

            try:
                with anyio.fail_after(SEMAPHORE_WAIT_SECONDS):
                    await semaphore.acquire()
            except TimeoutError:
                raise QuotaExceeded("Server is busy with other requests, please retry shortly.") from None
            try:
                with anyio.fail_after(TOOL_TIMEOUT_SECONDS):
                    if name in EXTRA_TOOL_NAMES:
                        await _verify_webhook_if_due()
                        result = await extra.handler(name)(args)
                    else:
                        result = await upstream.call_tool(name, args)
            finally:
                semaphore.release()

            result = _add_image_links(_truncate(list(result), settings.max_response_chars))
            upstream_error = any(
                isinstance(c, TextContent) and c.text.lstrip().lower().startswith("error") for c in result
            )
            audit("allowed", upstream_error=upstream_error)
            if upstream_error:
                return CallToolResult(content=[*result, _usage_line(ctx)], isError=True)
            return [*result, _usage_line(ctx)]
        except (PolicyDenied, QuotaExceeded) as exc:
            audit("denied", reason=str(exc)[:300])
            return _deny(f"Denied: {exc}", _usage_line(ctx))
        except TimeoutError:
            audit("timeout")
            return _deny(f"Onshape did not answer within {TOOL_TIMEOUT_SECONDS} s.", _usage_line(ctx))
        except Exception as exc:  # never leak internals to the client
            logger.exception(f"Unhandled error in tool {name}")
            audit("error", error=type(exc).__name__)
            return _deny("Internal server error while running the tool.", _usage_line(ctx))
        finally:
            _call_ctx.reset(token)

    security = None
    if settings.allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=[f"https://{h}" for h in settings.allowed_hosts],
        )

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=security,
        max_request_body_size=settings.max_request_bytes,
    )

    class _ASGI:
        """Wrap an ASGI callable so Starlette's Route passes it the raw scope."""

        def __init__(self, fn):
            self.fn = fn

        async def __call__(self, scope, receive, send):
            await self.fn(scope, receive, send)

    handle_streamable = _ASGI(session_manager.handle_request)

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    routes: list = [
        Route("/healthz", endpoint=health, methods=["GET"]),
        Route("/mcp", endpoint=handle_streamable, methods=["GET", "POST", "DELETE"]),
    ]
    if images is not None:
        routes.append(Route("/img/{name}", endpoint=images.serve, methods=["GET", "HEAD"]))
    if links is not None:
        routes.append(Route("/dl/{token}/{filename}", endpoint=links.serve, methods=["GET", "HEAD"]))

    if mapping is not None and settings.public_base_url:
        routes.append(Route("/onshape/webhook/{secret}", endpoint=make_receiver(mapping), methods=["POST"]))

        def _not_admin() -> Response | None:
            client = current_client.get()
            if client != ADMIN_LABEL:
                logger.warning(f"admin_denied client={json.dumps(client)}")
                return JSONResponse({"error": "admin token required"}, status_code=403)
            return None

        async def admin_webhook(request: Request) -> Response:
            """Admin token only: GET = status, POST = register the release webhook (once)."""
            if (denied := _not_admin()) is not None:
                return denied
            logger.info(f"AUDIT admin_webhook method={request.method}")
            ctx = {"cls": SYSTEM, "doc": None, "requests": 0, "blocked": 0}
            token = _call_ctx.set(ctx)
            try:
                if request.method == "POST" and mapping.meta("webhook_id") and not mapping.meta("webhook_meta_id") \
                        and not mapping.meta("webhook_meta_unsupported"):
                    # separate subscription for part-metadata changes (keeps the part-number index fresh)
                    cid = await extra._company()
                    url = f"{settings.public_base_url}/onshape/webhook/{mapping.webhook_secret()}"
                    try:
                        meta_hook = await upstream.client.post("/api/webhooks", data={
                            "companyId": cid, "events": [METADATA_EVENT], "url": url,
                            "options": {"collapseEvents": True}, "isTransient": False,
                            "description": "onshape-mcp-secure: part metadata changes",
                        })
                        mid = str(meta_hook.get("id") or "")
                        if re.fullmatch(r"[0-9a-f]{24}", mid):
                            mapping.set_meta("webhook_meta_id", mid)
                            logger.info("metadata webhook registered")
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code == 400:  # not available company-wide -> daily re-index
                            mapping.set_meta("webhook_meta_unsupported", "1")
                            logger.info("metadata webhook not supported for company scope")
                        else:
                            raise
                if request.method == "POST" and not mapping.meta("webhook_id"):
                    cid = await extra._company()
                    url = f"{settings.public_base_url}/onshape/webhook/{mapping.webhook_secret()}"
                    created = await upstream.client.post("/api/webhooks", data={
                        "companyId": cid, "events": RELEASE_EVENTS, "url": url,
                        "options": {"collapseEvents": False}, "isTransient": False,
                        "description": "onshape-mcp-secure: keep part/drawing mapping table fresh",
                    })
                    wid = str(created.get("id") or "")
                    if not re.fullmatch(r"[0-9a-f]{24}", wid):
                        return JSONResponse({"error": "Onshape did not return a webhook id"}, status_code=502)
                    mapping.set_meta("webhook_id", wid)
                    mapping.set_meta("webhook_verified_at", str(time.time()))
                    mapping.set_meta("webhook_dropped", "0")
                    logger.info("release webhook registered")
            except Exception as exc:  # noqa: BLE001
                status = getattr(getattr(exc, "response", None), "status_code", None)
                logger.warning(f"webhook registration failed: {type(exc).__name__} status={status}")
                return JSONResponse({"error": "registration failed", "onshapeStatus": status}, status_code=502)
            finally:
                _call_ctx.reset(token)
            return JSONResponse({
                "registered": bool(mapping.meta("webhook_id")),
                "metadataEvents": bool(mapping.meta("webhook_meta_id")),
                "active": mapping.webhook_active(),
                "lastEventAt": mapping.meta("webhook_last_event_at"),
                "onshapeRequests": ctx["requests"],
                "table": mapping.summary(),
            })

        routes.append(Route("/admin/webhook", endpoint=admin_webhook, methods=["GET", "POST"]))

        sync_state = {"last": 0.0}

        async def admin_sync(request: Request) -> Response:
            """Admin token only, max. once per 10 min: sync released revisions (+ part->drawing links)."""
            if (denied := _not_admin()) is not None:
                return denied
            if not re.fullmatch(r"\d{0,4}", request.query_params.get("maxParts", "")):
                return JSONResponse({"error": "maxParts must be an integer"}, status_code=400)
            wait = sync_state["last"] + ADMIN_SYNC_COOLDOWN - time.time()
            if wait > 0:
                return JSONResponse({"error": f"sync cooldown, retry in {int(wait) + 1} s"}, status_code=429)
            sync_state["last"] = time.time()
            logger.info(f"AUDIT admin_sync query={json.dumps(str(request.query_params))[:200]}")
            ctx = {"cls": READ, "doc": None, "requests": 0, "blocked": 0, "billed": 0}
            token = _call_ctx.set(ctx)
            try:
                q = request.query_params
                doc = q.get("document")  # documentId or exact document name for drawing links
                try:
                    max_parts = max(0, min(int(q.get("maxParts", "0") or 0), 200))
                except ValueError:
                    return JSONResponse({"error": "maxParts must be an integer"}, status_code=400)
                result: dict = {"revisions": await extra.sync_revisions()}
                if max_parts:
                    did = None
                    if doc:
                        did = doc if re.fullmatch(r"[0-9a-f]{24}", doc) else next(
                            (p["documentId"] for p in mapping.released_parts() if p.get("documentName") == doc), None)
                        if did is None:
                            return JSONResponse({"error": f"unknown document {doc!r}"}, status_code=400)
                    result["drawings"] = await extra.link_drawings(did, max_parts)
                result["onshapeCalls"] = ctx["billed"]
                result["table"] = mapping.summary()
                result["budget"] = budget.snapshot()
                return JSONResponse(result)
            except QuotaExceeded as exc:
                return JSONResponse({"error": str(exc), "onshapeCalls": ctx["billed"]}, status_code=429)
            finally:
                _call_ctx.reset(token)

        routes.append(Route("/admin/sync", endpoint=admin_sync, methods=["POST"]))

    if settings.enable_legacy_sse:
        sse = SseServerTransport(
            "/messages/", security_settings=security, max_request_body_size=settings.max_request_bytes
        )

        active_sse = [0]

        async def _sse(scope, receive, send):
            if active_sse[0] >= MAX_SSE_SESSIONS:
                await Response("too many open SSE sessions", status_code=503)(scope, receive, send)
                return
            active_sse[0] += 1
            try:
                async with sse.connect_sse(scope, receive, send) as streams:
                    await server.run(streams[0], streams[1], server.create_initialization_options())
            finally:
                active_sse[0] -= 1

        handle_sse = _ASGI(_sse)

        routes += [
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages/", app=sse.handle_post_message),
        ]

    def _cleanup_files() -> None:
        for store, fn in ((files, "_evict"), (links, "_cleanup"), (images, "_cleanup")):
            if store is not None:
                try:
                    getattr(store, fn)()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"cleanup failed: {exc}")

    async def _cleanup_loop() -> None:
        while True:
            _cleanup_files()
            await asyncio.sleep(1800)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        cleaner = asyncio.create_task(_cleanup_loop())  # also runs right at start (after auto-stop)
        async with session_manager.run():
            logger.info(
                f"onshape-mcp-secure {__version__} ready: write_mode={settings.write_mode}, "
                f"prefix={settings.write_allowed_name_prefix!r}, allow_delete={settings.allow_delete}, "
                f"featurescript={settings.allow_featurescript}, legacy_sse={settings.enable_legacy_sse}, "
                f"disabled_tools={sorted(settings.disabled_tools)}, clients={len(settings.auth_tokens)}, "
                f"budget={budget.snapshot()}"
            )
            yield
        cleaner.cancel()
        await upstream.client.close()

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BearerAuthMiddleware, tokens=settings.auth_tokens)
    return app


def main() -> None:
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()  # local runs only; the Docker image contains no .env
    try:
        settings = load_settings()
    except Exception as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
        ws="none",
        log_level="warning",
    )


if __name__ == "__main__":
    main()
