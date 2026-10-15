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
import types
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
from .config import ADMIN_LABEL, ConfigError, Settings, load_settings
from .policy import (
    CREATE_DOC,
    DELETE,
    FEATURESCRIPT,
    META,
    READ,
    SYSTEM,
    WRITE,
    EXTRA_TOOL_NAMES,
    TOOL_CLASSES,
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
SYNC_COOLDOWN_KEY = "admin_sync_last"  # mappings.db meta: survives auto-stop restarts (AD-8)

TOOL_TIMEOUT_SECONDS = 180
POLICY_TOOL = "get_server_policy"
SEMAPHORE_WAIT_SECONDS = 30
MAX_SSE_SESSIONS = 10
# upstream client methods that send a request (wrapped when present) and public methods that don't
CLIENT_REQUEST_METHODS = ("get", "post", "put", "patch", "delete")
CLIENT_NON_REQUEST_METHODS = frozenset({"close"})
# the open call scope (AD-3): {"cls": class, "doc": gated documentId, "attempts", "billed", "blocked", ...}
_call_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("call_ctx", default=None)


class DataDirError(ConfigError):
    """The data directory (/data) or a store on it cannot be used - the server must not start (AD-8)."""


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


def _audit(event: str, **fields: Any) -> None:
    """The only audit log format: one JSON line per tool call, admin action and job (AD-3)."""
    fields = {"event": event, "client": current_client.get(), **fields}
    logger.info("AUDIT " + json.dumps(fields, ensure_ascii=False, default=str))


@contextlib.contextmanager
def _call_scope(event: str, action: str, cls: str, document: str | None = None):
    """One call scope (actor, action, class, document) per tool call, admin action or job.

    Every Onshape request needs an open scope; on exit exactly one audit line is written.
    Callers set ctx["decision"] and add audit fields to ctx["audit"]. A nested scope (a job
    inside a tool call) audits itself; its billed calls also show in the outer usage line.
    """
    ctx = {"cls": cls, "doc": None, "attempts": 0, "billed": 0, "blocked": 0, "nested_billed": 0,
           "decision": "allowed", "audit": {}}
    parent = _call_ctx.get()
    token = _call_ctx.set(ctx)
    started = time.monotonic()
    try:
        yield ctx
    except BaseException:
        if ctx["decision"] == "allowed":
            ctx["decision"] = "error"
        raise
    finally:
        _call_ctx.reset(token)
        if parent is not None:
            parent["nested_billed"] += ctx["billed"] + ctx["nested_billed"]
        _audit(event, **{
            **ctx["audit"],  # extras first: they can never replace the fixed fields
            "action": action[:80],
            "cls": cls,
            "decision": ctx["decision"],
            "document": document[:64] if isinstance(document, str) else None,
            "onshapeCalls": ctx["billed"],
            "attempts": ctx["attempts"],
            "blocked": ctx["blocked"] or None,
            "ms": int((time.monotonic() - started) * 1000),
        })


def _check_client_methods(client_cls: type) -> list[str]:
    """Request methods to wrap; any other public upstream client method stops the start (AD-3)."""
    public = sorted(n for n in dir(client_cls) if not n.startswith("_") and callable(getattr(client_cls, n)))
    unknown = [n for n in public if n not in CLIENT_REQUEST_METHODS and n not in CLIENT_NON_REQUEST_METHODS]
    if unknown:
        raise RuntimeError(
            f"upstream client method(s) not covered by the call scope: {', '.join(unknown)} - "
            "add them to CLIENT_REQUEST_METHODS (wrapped) or CLIENT_NON_REQUEST_METHODS"
        )
    return [n for n in public if n in CLIENT_REQUEST_METHODS]


def _check_data_dir(settings: Settings) -> str:
    """The data directory must be writable, otherwise budget and mapping would silently vanish (AD-8)."""
    data_dir = os.path.dirname(settings.usage_file) or "."
    probe = os.path.join(data_dir, f".write-probe-{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        raise DataDirError(f"data directory {data_dir!r} is not writable: {reason}") from None
    return data_dir


def _open_store(what: str, factory):
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 - a store that cannot open stops the start
        raise DataDirError(f"cannot open {what}: {type(exc).__name__}: {exc}") from None


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


def _check_tool_registry(settings: Settings) -> None:
    """Fail at startup on duplicate own tool names and on DISABLED_TOOLS typos (AD-2, AD-10)."""
    names = [tool.name for tool in EXTRA_TOOLS]
    duplicates = sorted({n for n in names if names.count(n) > 1} | ({POLICY_TOOL} & set(names)))
    if duplicates:
        raise RuntimeError(f"duplicate tool names: {', '.join(duplicates)}")
    # every upstream tool is in TOOL_CLASSES (test_every_upstream_tool_is_classified); config.py
    # must not import policy, so the names are checked here
    unknown = sorted(settings.disabled_tools - set(TOOL_CLASSES) - EXTRA_TOOL_NAMES)
    if unknown:
        raise ConfigError(f"DISABLED_TOOLS contains unknown tool names: {', '.join(unknown)}")


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or load_settings()
    _configure_logging(settings.log_level)
    _check_tool_registry(settings)
    data_dir = _check_data_dir(settings)

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
            f"[Onshape-API: {ctx['billed'] + ctx['nested_billed']} Aufruf(e) für diese Anfrage · "
            + " · ".join([
                part("heute", b["today_utc"], b["daily_limit"]),
                part("Woche", b["this_week_utc"], b["weekly_limit"]),
                part("Monat", b["this_month_utc"], b["monthly_limit"]),
                part("Jahr", b["this_year_utc"], b["yearly_limit"]),
            ]) + "]"
        ))

    limiter = RateLimiter(settings.tool_calls_per_minute)
    semaphore = asyncio.Semaphore(settings.max_concurrent_calls)

    async def _send(method_name: str, path: str, call):
        """The only way to Onshape: open call scope, HTTP guard, budget reserve, refund if unbilled (AD-3)."""
        ctx = _call_ctx.get()
        if ctx is None:
            logger.warning(f"blocked {method_name.upper()} outside a call scope")
            raise PolicyDenied("Onshape request outside a call scope.")
        try:
            check_onshape_request(method_name, str(path), ctx["cls"], ctx["doc"])
        except PolicyDenied:
            ctx["blocked"] += 1
            logger.warning(f"blocked {method_name.upper()} outside tool permission (tool class {ctx['cls']})")
            raise
        await budget.reserve()
        ctx["attempts"] += 1
        ctx["billed"] += 1
        try:
            return await call()
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            if isinstance(exc, httpx.TransportError) or exc.response.status_code >= 400:
                await budget.refund()  # Onshape doesn't bill error answers
                ctx["billed"] -= 1
            raise

    def _metered(method_name, method):
        async def wrapper(path, *args, **kwargs):
            return await _send(method_name, path, lambda: method(path, *args, **kwargs))

        wrapper.secure_metered = True
        return wrapper

    # Every Onshape HTTP request goes through the shared upstream client: meter each request
    # method; any other public method stops the start, so new upstream methods can't bypass it.
    client_cls = type(upstream.client)
    for method_name in _check_client_methods(client_cls):
        # bind the class function, never an already wrapped attribute (create_app may run twice)
        original = types.MethodType(getattr(client_cls, method_name), upstream.client)
        setattr(upstream.client, method_name, _metered(method_name, original))

    async def fetch_document_name(document_id: str) -> str:
        data = await upstream.client.get(f"/api/v6/documents/{document_id}")
        return str(data.get("name", ""))

    async def get_bytes(path: str, max_bytes: int) -> bytes:
        """Binary GET (file downloads) through the same guard + budget as every other request."""
        client = upstream.client

        async def download() -> bytes:
            client._ensure_client()
            headers = {"Authorization": client._get_auth_header(), "Accept": "application/octet-stream"}
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

        return await _send("get", path, download)

    access = DocumentAccess(settings, fetch_document_name)

    # Stores on the data directory: if one cannot open, the server does not start (AD-8).
    images: ImageStore | None = None
    links: DownloadLinks | None = None
    kv = _open_store("KV cache", lambda: KVCache(os.path.join(data_dir, "cache.json")))
    files = _open_store("file cache", lambda: FileCache(
        os.path.join(data_dir, "files"), settings.cache_max_mb * 1024 * 1024, settings.file_cache_hours * 3600
    ))
    if settings.image_links and settings.public_base_url:
        image_dir = settings.image_dir or os.path.join(data_dir, "images")
        images = _open_store("image store", lambda: ImageStore(
            image_dir, settings.image_ttl_hours * 3600, settings.public_base_url
        ))
        links = _open_store("download links", lambda: DownloadLinks(
            os.path.join(data_dir, "links"), settings.image_ttl_hours * 3600, settings.public_base_url
        ))
    mapping = _open_store("mapping table", lambda: MappingStore(os.path.join(data_dir, "mappings.db")))
    extra = ExtraTools(
        upstream.client, access.check_read, get_bytes=get_bytes, kv=kv, files=files, links=links, mapping=mapping,
        web_base_url=settings.onshape_base_url,
    )

    async def _verify_webhook_if_due() -> None:
        """At most weekly: is Onshape still delivering release events? (1 GET, only when a tool runs)."""
        if not mapping.meta("webhook_id"):
            return
        verified = float(mapping.meta("webhook_verified_at") or 0)
        if time.time() - verified < 7 * 86400:
            return
        wid = mapping.meta("webhook_id")
        with _call_scope("job", "verify_webhook", SYSTEM) as job:
            try:
                info = await upstream.client.get(f"/api/webhooks/{wid}")
            except Exception as exc:  # noqa: BLE001 - the tool call must not fail because of this check
                status = getattr(getattr(exc, "response", None), "status_code", None)
                job["decision"] = "error"
                job["audit"] = {"error": type(exc).__name__, "onshapeStatus": status}
                if status == 404:
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
            upstream_tools = await upstream.list_tools()
            names = [tool.name for tool in upstream_tools] + [tool.name for tool in EXTRA_TOOLS] + [POLICY_TOOL]
            if duplicates := sorted({n for n in names if names.count(n) > 1}):
                raise RuntimeError(f"duplicate tool names: {', '.join(duplicates)}")
            for tool in upstream_tools:
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
        cls = tool_class(name) if name != POLICY_TOOL else META
        doc_id = (arguments or {}).get("documentId") if isinstance(arguments, dict) else None
        doc_id = doc_id if isinstance(doc_id, str) and len(doc_id) <= 64 else None
        with _call_scope("tool_call", name, cls, doc_id) as ctx:
            return await _run_tool(ctx, name, cls, arguments)

    async def _run_tool(ctx: dict, name: str, cls: str | None, arguments: dict | None) -> Any:
        try:
            limiter.hit(current_client.get())
            if name == POLICY_TOOL:
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
            ctx["audit"]["upstream_error"] = upstream_error
            if upstream_error:
                return CallToolResult(content=[*result, _usage_line(ctx)], isError=True)
            return [*result, _usage_line(ctx)]
        except (PolicyDenied, QuotaExceeded) as exc:
            ctx["decision"], ctx["audit"]["reason"] = "denied", str(exc)[:300]
            return _deny(f"Denied: {exc}", _usage_line(ctx))
        except TimeoutError:
            ctx["decision"] = "timeout"
            return _deny(f"Onshape did not answer within {TOOL_TIMEOUT_SECONDS} s.", _usage_line(ctx))
        except Exception as exc:  # never leak internals to the client
            logger.exception(f"Unhandled error in tool {name}")
            ctx["decision"], ctx["audit"]["error"] = "error", type(exc).__name__
            return _deny("Internal server error while running the tool.", _usage_line(ctx))

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

    if settings.public_base_url:
        routes.append(Route("/onshape/webhook/{secret}", endpoint=make_receiver(mapping), methods=["POST"]))

        def _not_admin(ctx: dict) -> Response | None:
            if current_client.get() != ADMIN_LABEL:
                ctx["decision"], ctx["audit"]["reason"] = "denied", "admin token required"
                return JSONResponse({"error": "admin token required"}, status_code=403)
            return None

        class _WebhookIdMissing(Exception):
            pass

        async def _register_webhooks() -> None:
            if mapping.meta("webhook_id") and not mapping.meta("webhook_meta_id") \
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
            if not mapping.meta("webhook_id"):
                cid = await extra._company()
                url = f"{settings.public_base_url}/onshape/webhook/{mapping.webhook_secret()}"
                created = await upstream.client.post("/api/webhooks", data={
                    "companyId": cid, "events": RELEASE_EVENTS, "url": url,
                    "options": {"collapseEvents": False}, "isTransient": False,
                    "description": "onshape-mcp-secure: keep part/drawing mapping table fresh",
                })
                wid = str(created.get("id") or "")
                if not re.fullmatch(r"[0-9a-f]{24}", wid):
                    raise _WebhookIdMissing()
                mapping.set_meta("webhook_id", wid)
                mapping.set_meta("webhook_verified_at", str(time.time()))
                mapping.set_meta("webhook_dropped", "0")
                logger.info("release webhook registered")

        async def admin_webhook(request: Request) -> Response:
            """Admin token only: GET = status, POST = register the release webhook (once)."""
            with _call_scope("admin", "admin_webhook", SYSTEM) as ctx:
                ctx["audit"]["method"] = request.method
                if (denied := _not_admin(ctx)) is not None:
                    return denied
                try:
                    if request.method == "POST":
                        await _register_webhooks()
                except _WebhookIdMissing:
                    ctx["decision"], ctx["audit"]["reason"] = "error", "no webhook id"
                    return JSONResponse({"error": "Onshape did not return a webhook id"}, status_code=502)
                except Exception as exc:  # noqa: BLE001
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    ctx["decision"] = "error"
                    ctx["audit"].update(error=type(exc).__name__, onshapeStatus=status)
                    logger.warning(f"webhook registration failed: {type(exc).__name__} status={status}")
                    return JSONResponse({"error": "registration failed", "onshapeStatus": status}, status_code=502)
                return JSONResponse({
                    "registered": bool(mapping.meta("webhook_id")),
                    "metadataEvents": bool(mapping.meta("webhook_meta_id")),
                    "active": mapping.webhook_active(),
                    "lastEventAt": mapping.meta("webhook_last_event_at"),
                    "onshapeCalls": ctx["billed"],
                    "table": mapping.summary(),
                })

        routes.append(Route("/admin/webhook", endpoint=admin_webhook, methods=["GET", "POST"]))

        def _sync_wait() -> float:
            """Seconds until the next /admin/sync; the last start lives in mappings.db (AD-8)."""
            try:
                last = float(mapping.meta(SYNC_COOLDOWN_KEY) or 0)
            except ValueError:
                last = 0.0
            wait = last + ADMIN_SYNC_COOLDOWN - time.time()
            return wait if 0 < wait <= ADMIN_SYNC_COOLDOWN else 0  # a start time in the future is ignored

        async def admin_sync(request: Request) -> Response:
            """Admin token only, max. once per 10 min: sync released revisions (+ part->drawing links)."""
            q = request.query_params
            doc = q.get("document")  # documentId or exact document name for drawing links
            with _call_scope("admin", "admin_sync", READ, doc) as ctx:
                ctx["audit"]["maxParts"] = q.get("maxParts", "")[:8]
                if (denied := _not_admin(ctx)) is not None:
                    return denied
                if not re.fullmatch(r"\d{0,4}", q.get("maxParts", "")):
                    ctx["decision"], ctx["audit"]["reason"] = "denied", "bad maxParts"
                    return JSONResponse({"error": "maxParts must be an integer"}, status_code=400)
                if (wait := _sync_wait()) > 0:
                    ctx["decision"], ctx["audit"]["reason"] = "denied", "cooldown"
                    return JSONResponse({"error": f"sync cooldown, retry in {int(wait) + 1} s"}, status_code=429)
                mapping.set_meta(SYNC_COOLDOWN_KEY, str(time.time()))
                max_parts = min(int(q.get("maxParts") or 0), 200)
                try:
                    result: dict = {"revisions": await extra.sync_revisions()}
                    if max_parts:
                        did = None
                        if doc:
                            did = doc if re.fullmatch(r"[0-9a-f]{24}", doc) else next(
                                (p["documentId"] for p in mapping.released_parts() if p.get("documentName") == doc),
                                None)
                            if did is None:
                                ctx["decision"], ctx["audit"]["reason"] = "denied", "unknown document"
                                return JSONResponse({"error": f"unknown document {doc!r}"}, status_code=400)
                        result["drawings"] = await extra.link_drawings(did, max_parts)
                except QuotaExceeded as exc:
                    ctx["decision"], ctx["audit"]["reason"] = "denied", str(exc)[:300]
                    return JSONResponse({"error": str(exc), "onshapeCalls": ctx["billed"]}, status_code=429)
                except Exception as exc:  # noqa: BLE001 - same answer and audit as /admin/webhook
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    ctx["decision"] = "error"
                    ctx["audit"].update(error=type(exc).__name__, onshapeStatus=status)
                    logger.warning(f"admin sync failed: {type(exc).__name__} status={status}")
                    return JSONResponse({"error": "sync failed", "onshapeStatus": status,
                                         "onshapeCalls": ctx["billed"]}, status_code=502)
                result["onshapeCalls"] = ctx["billed"]
                result["table"] = mapping.summary()
                result["budget"] = budget.snapshot()
                return JSONResponse(result)

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
        await _published_tools()  # duplicate tool names stop the server before it accepts requests
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
    try:
        app = create_app(settings)
    except ConfigError as exc:  # e.g. unknown DISABLED_TOOLS names
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
        ws="none",
        log_level="warning",
        access_log=False,  # access lines would carry webhook secrets / link tokens (AD-10, AD-13)
    )


if __name__ == "__main__":
    main()
