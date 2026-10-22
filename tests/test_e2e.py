"""End-to-end: real uvicorn server + real MCP client, Onshape API mocked with httpx."""

import base64
import json
import os
import re
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from conftest import ADMIN_TOKEN, AI_DOC, EL, LISTED_DOC, OTHER_TOKEN, PROD_DOC, TOKEN, WS, make_settings

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode()
NEW_DOC = "f" * 24
DOC_NAMES = {AI_DOC: "AI-Bracket", PROD_DOC: "GEN3 Leg Assembly", LISTED_DOC: "Sandbox"}


class FakeOnshape:
    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.translations: dict[str, dict] = {}
        self.translation_counter = 0
        self.active_polls = 0  # how many polls answer ACTIVE before DONE
        self.where_used_empty = False
        self.index_calls = 0
        self.filter0_resolves = False
        self.sync_pages: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path
        self.requests.append((request.method, path, body))
        assert request.headers["authorization"].startswith("Basic ")
        m = re.fullmatch(r"/api/v6/documents/([0-9a-f]{24})", path)
        if m and request.method == "GET":
            doc = m.group(1)
            if doc not in DOC_NAMES and doc != NEW_DOC:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=_doc(doc, DOC_NAMES.get(doc, "AI-new")))
        if path == "/api/v10/documents" and request.method == "POST":
            return httpx.Response(200, json=_doc(NEW_DOC, body["name"]))
        if path.startswith("/api/") and path.endswith("/translations") and request.method == "POST":
            self.translation_counter += 1
            tid = f"tr{self.translation_counter}"
            self.translations[tid] = {"polls": 0, "kind": "pdf" if "/drawings/" in path else "step"}
            return httpx.Response(200, json={"id": tid, "requestState": "ACTIVE"})
        m = re.fullmatch(r"/api/translations/(tr\d+)", path)
        if m:
            t = self.translations[m.group(1)]
            t["polls"] += 1
            if t["polls"] <= self.active_polls:
                return httpx.Response(200, json={"id": m.group(1), "requestState": "ACTIVE"})
            return httpx.Response(200, json={"id": m.group(1), "requestState": "DONE",
                                             "resultExternalDataIds": [f"ext{t['kind']}"]})
        m = re.fullmatch(r"/api/documents/d/[0-9a-f]{24}/externaldata/ext(step|pdf)", path)
        if m:
            body = b"ISO-10303-21;\nHEADER;" if m.group(1) == "step" else b"%PDF-1.7 fake"
            return httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})
        if re.fullmatch(r"/api/documents/d/[0-9a-f]{24}/v/9{24}/elements", path):
            return httpx.Response(200, json=[{"id": "7" * 24, "name": "Deckel Zeichnung", "elementType": "DRAWING"}])
        if path == f"/api/parts/d/{PROD_DOC}/w/{WS}":
            self.index_calls += 1
            return httpx.Response(200, json=[
                {"name": "Anschluss Mitnehmer", "partNumber": "PRT-000583", "elementId": EL, "partId": "JKD",
                 "state": "IN_PROGRESS"},
                {"name": "Anschluss Mitnehmer spiegel", "partNumber": None, "elementId": EL, "partId": "JLD"},
            ])
        if path == f"/api/parts/d/{PROD_DOC}/w/{WS}/e/{EL}":
            return httpx.Response(200, json=[{"name": "Deckel", "partId": "JHD", "partNumber": "PRT-000593",
                                              "revision": "A", "state": "RELEASED"}])
        if re.fullmatch(r"/api/v6/webhooks/e{24}", path) and request.method == "POST":
            return httpx.Response(200, json={"id": "e" * 24})
        if path == "/api/revisions/companies/c0mpany":
            q = request.url.params
            self.sync_pages.append(dict(q))
            if q.get("page") == "2":
                return httpx.Response(200, json={"items": [
                    {"partNumber": "DRW-000635", "revision": "B", "elementType": 2, "name": "Deckel",
                     "documentId": LISTED_DOC, "versionId": "2" * 24, "elementId": "1" * 24,
                     "releaseCreatedDate": "2026-03-01T00:00:00Z", "createdAt": "2026-03-01T00:00:00Z"}]})
            if q.get("after", "").startswith("2026-03"):
                return httpx.Response(200, json={"items": []})
            return httpx.Response(200, json={
                "items": [{"partNumber": "PRT-000900", "revision": "A", "elementType": 0, "name": "Bracket",
                           "documentId": PROD_DOC, "versionId": "9" * 24, "elementId": EL, "partId": "JHD",
                           "documentName": "Robot Gen3", "createdAt": "2026-02-01T00:00:00Z"}],
                "next": "https://cad.onshape.com/api/revisions/companies/c0mpany?latestOnly=true&limit=100&page=2"})
        if path == "/api/v6/webhooks" and request.method == "POST":
            return httpx.Response(200, json={"id": "e" * 24})
        if path == "/api/companies":
            return httpx.Response(200, json={"items": [{"id": "c0mpany"}]})
        m = re.fullmatch(r"/api/revisions/c/c0mpany/partnumber/(.+)", path)
        if m:
            pn, et = m.group(1), request.url.params.get("elementType")
            if pn == "PRT-000700" and et == "2":  # released drawing carrying the part's number
                return httpx.Response(200, json={
                    "partNumber": pn, "revision": "B", "name": "Halter Zeichnung", "elementType": 2,
                    "documentId": AI_DOC, "versionId": "8" * 24, "elementId": "6" * 24})
            if pn not in ("PRT-000593", "PRT-000700", "PRT-000800", "PRT-000900") or et == "2":
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={
                "partNumber": pn, "revision": "A", "name": "Bracket", "elementType": 0,
                "documentId": PROD_DOC, "versionId": "9" * 24, "elementId": EL, "partId": "JHD",
                "documentName": "Robot Gen3", "configuration": "", "isObsolete": False,
                "releaseId": "5" * 24 if pn == "PRT-000800" else None})
        if path == "/api/v17/productstructure/whereused":
            assert request.url.params["useLatestVersion"] == "false" and "documentId" not in request.url.params
            pn = request.url.params.get("partNumber")
            if pn != "PRT-000583":
                return httpx.Response(200, json={"items": []})
            return httpx.Response(200, json={"items": [  # an assembly that uses the part
                {"jsonType": "product-element", "elementType": 1, "peerPartNumber": pn, "peerDocumentId": PROD_DOC,
                 "peerElementId": EL, "peerPartId": "JKD", "peerPartName": "Anschluss Mitnehmer",
                 "documentId": LISTED_DOC, "versionId": "0" * 24, "elementId": "1" * 24}]})
        if path == "/api/productstructure/whereused":
            q = request.url.params
            assert q["limitToTypes"] == "2" and q["versionId"] == "9" * 24 and q["partId"] == "JHD"
            if q["documentId"] != PROD_DOC or self.where_used_empty:
                return httpx.Response(200, json={"items": []})
            if q.get("filter") == "0":
                if not self.filter0_resolves:
                    return httpx.Response(200, json={"items": []})
                return httpx.Response(200, json={"items": [
                    {"jsonType": "product-element", "elementType": 2, "documentId": LISTED_DOC,
                     "versionId": "2" * 24, "elementId": "1" * 24, "partNumber": "DRW-000635", "revision": "B"},
                ]})
            # real Onshape shape (default filter): the drawing tab, without its own number/revision
            return httpx.Response(200, json={"items": [
                {"jsonType": "product-element", "elementType": 2, "documentId": LISTED_DOC, "versionId": "0" * 24,
                 "elementId": "1" * 24, "partNumber": None, "revision": None, "peerPartNumber": "PRT-000900"},
            ]})
        if path == f"/api/revisions/companies/c0mpany/d/{LISTED_DOC}/v/{'0' * 24}/e/{'1' * 24}":
            assert request.url.params["elementType"] == "2"
            rev = {"elementType": 2, "documentId": LISTED_DOC, "elementId": "1" * 24, "partNumber": "DRW-000635",
                   "name": "Deckel", "isObsolete": False}
            return httpx.Response(200, json={"items": [
                {**rev, "revision": "A", "versionId": "3" * 24, "releaseCreatedDate": "2026-01-01T00:00:00Z"},
                {**rev, "revision": "B", "versionId": "2" * 24, "releaseCreatedDate": "2026-03-01T00:00:00Z"},
            ]})
        if path == "/api/releasepackages/" + "5" * 24:
            return httpx.Response(200, json={"items": [
                {"id": "it1", "elementType": 0, "partId": "JHD", "elementId": EL, "documentId": PROD_DOC},
                {"id": "it2", "elementType": 2, "documentId": LISTED_DOC, "versionId": "4" * 24,
                 "elementId": "3" * 24, "referenceIds": ["it1"]},
            ]})
        if path.endswith("/shadedviews"):
            return httpx.Response(200, json={"images": [PNG]})
        if path.endswith("/features") and request.method == "POST":
            return httpx.Response(200, json={"feature": {"featureId": "Fabc_1"}})
        if path.endswith("/features") and request.method == "GET":
            return httpx.Response(200, json={"features": [], "defaultFeatures": []})
        return httpx.Response(200, json={})


def _doc(doc_id, name):
    return {
        "id": doc_id,
        "name": name,
        "createdAt": "2026-01-01T00:00:00Z",
        "modifiedAt": "2026-01-01T00:00:00Z",
        "ownerId": "owner",
        "public": False,
        "defaultWorkspace": {"id": WS},
    }


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    import onshape_secure.extra_tools as et

    monkeypatch.setattr(et, "POLL_DELAYS_NEW", (0, 0, 0))
    monkeypatch.setattr(et, "POLL_DELAYS_RESUME", (0, 0))


@pytest.fixture
def server(tmp_path):
    from onshape_secure.app import create_app
    from onshape_mcp import server as upstream

    def start(**overrides):
        overrides.setdefault("image_links", True)
        overrides.setdefault("public_base_url", "http://127.0.0.1")
        settings = make_settings(tmp_path, **overrides)
        app = create_app(settings)
        fake = FakeOnshape()
        upstream.client._client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        upstream.client._own_client = True
        port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        srv = uvicorn.Server(config)
        thread = threading.Thread(target=srv.run, daemon=True)
        thread.start()
        for _ in range(100):
            if srv.started:
                break
            time.sleep(0.05)
        started.append((srv, thread))
        return f"http://127.0.0.1:{port}", fake

    started = []
    yield start
    for srv, thread in started:
        srv.should_exit = True
        thread.join(timeout=5)


async def _call(base, name, args=None, token=TOKEN):
    async with streamablehttp_client(f"{base}/mcp", headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return await session.call_tool(name, args or {})


async def _tools(base, token=TOKEN):
    async with streamablehttp_client(f"{base}/mcp", headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            return init, (await session.list_tools()).tools


def _text(result):
    return "\n".join(c.text for c in result.content if c.type == "text")


def _json(result):
    return json.loads(next(c.text for c in result.content if c.type == "text"))


def _writes(fake):
    return [r for r in fake.requests if r[0] in ("POST", "DELETE")]


# --- authentication -------------------------------------------------------


def test_health_is_public(server):
    base, _ = server()
    assert httpx.get(f"{base}/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}])
def test_mcp_requires_token(server, headers):
    base, _ = server()
    for path in ("/mcp", "/sse", "/messages/"):
        resp = httpx.post(f"{base}{path}", headers=headers, json={})
        assert resp.status_code == 401, path


def test_legacy_sse_not_routed_when_off(server):
    base, _ = server(enable_legacy_sse=False)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert httpx.get(f"{base}/sse", headers=auth).status_code == 404
    assert httpx.post(f"{base}/messages/", headers=auth, json={}).status_code == 404


def test_x_api_key_header_accepted(server):
    base, _ = server()
    resp = httpx.post(
        f"{base}/mcp",
        headers={"X-API-Key": OTHER_TOKEN, "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert resp.status_code == 200


# --- tool listing ---------------------------------------------------------


async def test_tool_list_is_sanitized(server):
    base, _ = server()
    init, tools = await _tools(base)
    names = {t.name for t in tools}
    assert "get_server_policy" in names and "create_extrude" in names
    assert "export_part_studio" not in names  # disabled by default
    for t in tools:
        props = t.inputSchema.get("properties", {})
        assert "outputPath" not in props and "isPublic" not in props
    assert "AI-" in init.instructions


async def test_read_only_mode_hides_write_tools(server):
    base, _ = server(write_mode="off")
    _, tools = await _tools(base)
    names = {t.name for t in tools}
    assert "create_extrude" not in names and "delete_feature" not in names
    assert "create_document" not in names and "get_parts" in names


# --- policy enforcement ---------------------------------------------------


async def test_read_allowed_on_any_document(server):
    base, _ = server()
    result = await _call(base, "get_document", {"documentId": PROD_DOC})
    assert not result.isError and "GEN3 Leg Assembly" in _text(result)


async def test_write_denied_on_production_document(server):
    base, fake = server()
    args = {"documentId": PROD_DOC, "workspaceId": WS, "elementId": EL, "name": "x", "expression": "5 mm"}
    result = await _call(base, "set_variable", args)
    assert result.isError and "not allowed" in _text(result)
    assert _writes(fake) == []


@pytest.mark.parametrize("doc", [AI_DOC, LISTED_DOC])
async def test_write_allowed_on_prefixed_or_listed_document(server, doc):
    base, fake = server()
    args = {"documentId": doc, "workspaceId": WS, "elementId": EL, "name": "Sketch", "centerX": 0,
            "centerY": 0, "radius": 1, "plane": "Front"}
    result = await _call(base, "create_sketch_circle", args)
    assert not result.isError, _text(result)
    assert any(m == "POST" and f"/d/{doc}/" in p for m, p, _ in fake.requests)


async def test_create_document_is_private_and_prefixed(server):
    base, fake = server()
    result = await _call(base, "create_document", {"name": "Bracket", "isPublic": True})
    assert not result.isError, _text(result)
    body = next(b for m, p, b in fake.requests if p == "/api/v10/documents")
    assert body["isPublic"] is False and body["name"] == "AI-Bracket"


async def test_delete_feature_path_traversal_blocked(server):
    base, fake = server()
    args = {"documentId": AI_DOC, "workspaceId": WS, "elementId": EL,
            "featureId": "../../../../../../documents/" + PROD_DOC}
    result = await _call(base, "delete_feature", args)
    assert result.isError
    assert not any(m == "DELETE" for m, _, _ in fake.requests)


async def test_delete_feature_allowed_on_ai_document(server):
    base, fake = server()
    args = {"documentId": AI_DOC, "workspaceId": WS, "elementId": EL, "featureId": "Fabc_1"}
    result = await _call(base, "delete_feature", args)
    assert not result.isError, _text(result)
    assert any(m == "DELETE" and p.endswith("/featureid/Fabc_1") for m, p, _ in fake.requests)


async def test_delete_disabled_by_flag(server):
    base, fake = server(allow_delete=False)
    args = {"documentId": AI_DOC, "workspaceId": WS, "elementId": EL, "featureId": "Fabc_1"}
    result = await _call(base, "delete_feature", args)
    assert result.isError
    assert not any(m == "DELETE" for m, _, _ in fake.requests)


@pytest.mark.parametrize("bad", ["../" + "a" * 21, "A" * 24, "a" * 23, PROD_DOC + "/x"])
async def test_invalid_document_ids_rejected(server, bad):
    base, fake = server()
    result = await _call(base, "get_document", {"documentId": bad})
    assert result.isError
    assert fake.requests == []


async def test_screenshot_output_path_is_ignored(server, tmp_path):
    base, _ = server()
    target = tmp_path / "pwned.png"
    args = {"documentId": PROD_DOC, "workspaceId": WS, "elementId": EL, "outputPath": str(target),
            "outputWidth": 99999}
    result = await _call(base, "capture_part_studio_screenshot", args)
    assert not result.isError, _text(result)
    assert any(c.type == "image" for c in result.content)
    assert not target.exists()


async def test_read_restriction(server):
    base, _ = server(read_allowed_document_ids=frozenset({LISTED_DOC}))
    denied = await _call(base, "get_document", {"documentId": PROD_DOC})
    assert denied.isError
    allowed = await _call(base, "get_document", {"documentId": AI_DOC})  # prefixed => allowed
    assert not allowed.isError
    _, tools = await _tools(base)
    names = {t.name for t in tools}
    assert not names & {"list_documents", "search_documents", "show_mapping_table", "search_part_index",
                        "export_mapping_table"}
    assert "find_part_by_number" in names


async def test_unknown_tool_denied(server):
    base, _ = server()
    result = await _call(base, "drop_everything", {})
    assert result.isError


# --- budget / rate limit / audit -------------------------------------------


async def test_daily_budget_enforced_and_persisted(server, tmp_path):
    base, fake = server(onshape_daily_limit=2)
    for _ in range(2):
        assert not (await _call(base, "get_document", {"documentId": PROD_DOC})).isError
    result = await _call(base, "get_document", {"documentId": PROD_DOC})
    assert result.isError and "budget" in _text(result).lower()
    assert len(fake.requests) == 2
    usage = json.loads((tmp_path / "usage.json").read_text())
    assert usage["day_count"] == 2


async def test_rate_limit(server):
    base, _ = server(tool_calls_per_minute=2)
    await _call(base, "get_server_policy")
    await _call(base, "get_server_policy")
    result = await _call(base, "get_server_policy")
    assert result.isError and "Rate limit" in _text(result)
    # a different client has its own window
    assert not (await _call(base, "get_server_policy", token=OTHER_TOKEN)).isError


async def test_policy_tool(server):
    base, _ = server()
    info = _json(await _call(base, "get_server_policy"))
    assert info["write_mode"] == "allowlist" and info["new_documents_are_private"] is True


async def test_budget_warning_from_80_percent(server):
    base, _ = server(onshape_monthly_limit=5)
    for _ in range(3):
        result = await _call(base, "get_document", {"documentId": PROD_DOC})
    assert "Monat 3/5 ·" in _text(result) and "WARNUNG" not in _text(result)  # 60 %
    assert _json(await _call(base, "get_server_policy"))["budget_warning"] is None
    result = await _call(base, "get_document", {"documentId": PROD_DOC})  # 4/5 = 80 %
    line = _text(result).splitlines()[-1]
    assert line.startswith("[Onshape-API: 1 Aufruf(e)") and line.endswith("]")
    assert "· WARNUNG Onshape-Budget fast verbraucht: Monat 4/5 (80 %)]" in line
    policy = await _call(base, "get_server_policy")
    info = _json(policy)
    assert "Monat 4/5 (80 %)" in info["budget_warning"] and info["budget_warning_percent"] == 80
    assert "WARNUNG" in _text(policy).splitlines()[-1]
    await _call(base, "get_document", {"documentId": PROD_DOC})  # 5/5 = 100 %
    denied = await _call(base, "get_document", {"documentId": PROD_DOC})
    line = _text(denied).splitlines()[-1]
    assert denied.isError and "Monat 5/5 (100 %)" in line and line.endswith("]")


async def test_yearly_budget_warning_at_79_and_80_percent(server, tmp_path):
    from onshape_secure.quota import _now_keys

    day, week, month, year = _now_keys()
    (tmp_path / "usage.json").write_text(json.dumps({
        "day": day, "day_count": 0, "week": week, "week_count": 0, "month": month, "month_count": 0,
        "year": year, "year_count": 78, "attempts": 0,
    }))
    base, _ = server(onshape_yearly_limit=100)
    result = await _call(base, "get_document", {"documentId": PROD_DOC})  # 79/100
    assert "Jahr 79/100]" in _text(result) and "WARNUNG" not in _text(result)
    result = await _call(base, "get_document", {"documentId": PROD_DOC})  # 80/100
    assert _text(result).splitlines()[-1].endswith("· WARNUNG Onshape-Budget fast verbraucht: Jahr 80/100 (80 %)]")
    assert "Jahr 80/100 (80 %)" in _json(await _call(base, "get_server_policy"))["budget_warning"]


async def test_no_budget_warning_without_limits(server):
    base, _ = server(onshape_daily_limit=0, onshape_weekly_limit=0, onshape_monthly_limit=0, onshape_yearly_limit=0)
    result = await _call(base, "get_document", {"documentId": PROD_DOC})
    assert "WARNUNG" not in _text(result)
    assert _json(await _call(base, "get_server_policy"))["budget_warning"] is None


# --- legacy SSE transport ---------------------------------------------------


async def test_legacy_sse_transport(server):
    base, _ = server()
    async with sse_client(f"{base}/sse", headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            result = await session.call_tool("get_document", {"documentId": PROD_DOC})
            assert not result.isError


# --- regression tests from the security review -------------------------------

FACE_ARGS = {"workspaceId": WS, "elementId": EL, "instanceId": "Mabc", "faceId": "JHD"}


async def test_face_cs_tool_cannot_touch_production(server):
    base, fake = server()
    result = await _call(base, "get_face_coordinate_system", {"documentId": PROD_DOC, **FACE_ARGS})
    assert result.isError
    assert _writes(fake) == []


async def test_face_cs_hidden_when_read_only(server):
    base, _ = server(write_mode="off")
    _, tools = await _tools(base)
    assert "get_face_coordinate_system" not in {t.name for t in tools}


async def test_failed_requests_are_refunded_like_onshape_bills(server):
    base, fake = server(onshape_daily_limit=2)
    for i in range(5):
        await _call(base, "get_document", {"documentId": "9" * 23 + str(i)})
    assert len(fake.requests) == 5  # 404s are not billed by Onshape ...
    info = _json(await _call(base, "get_server_policy"))
    assert info["onshape_api_budget"]["today_utc"] == 0  # ... so they don't eat the budget either


async def test_budget_not_overshot_by_concurrency(server):
    import asyncio

    from onshape_mcp import server as upstream

    base, fake = server(onshape_daily_limit=1)

    async def slow(request):
        await asyncio.sleep(0.3)
        return fake(request)

    upstream.client._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    await asyncio.gather(*[_call(base, "get_document", {"documentId": PROD_DOC}) for _ in range(4)])
    assert len(fake.requests) == 1


async def test_credentials_never_logged(server, capfd):
    from onshape_mcp import server as upstream

    base, _ = server(onshape_access_key="AK" * 12, onshape_secret_key="SK" * 24)

    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)

    upstream.client._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    result = await _call(base, "get_document", {"documentId": PROD_DOC})
    assert result.isError
    out = "".join(capfd.readouterr())
    secret_b64 = base64.b64encode(f"{'AK' * 12}:{'SK' * 24}".encode()).decode()
    assert "Unexpected error" in out or "get_document" in out
    assert secret_b64 not in out and "SK" * 24 not in out


def test_auth_failure_log_is_escaped(server, capfd):
    base, _ = server()
    httpx.post(f"{base}/mcp%0a2026-01-01 AUDIT forged", json={})
    out = "".join(capfd.readouterr())
    assert "auth_failed" in out
    assert "\n2026-01-01 AUDIT forged" not in out


def test_auth_failure_log_hides_secret_paths(server, capfd):
    base, _ = server()
    secret = "S3cr3tPathValue" + "x" * 30
    httpx.get(f"{base}/onshape/webhook/{secret}")  # GET is not the public webhook route
    httpx.post(f"{base}/img/{secret}.png")
    httpx.post(f"{base}/dl/{secret}/part.step")
    out = "".join(capfd.readouterr())
    assert out.count("auth_failed") == 3
    assert secret not in out and "S3cr3tP" not in out
    for prefix in ("/onshape/webhook/", "/img/", "/dl/"):
        assert f'"{prefix}S3cr3t..."' in out


# --- part numbers & image links --------------------------------------------------


async def test_find_part_by_number(server):
    base, fake = server()
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert not result.isError, _text(result)
    info = _json(result)
    assert info["documentName"] == "Robot Gen3" and info["revision"] == "A"
    assert info["onshapeUrl"] == f"https://cad.onshape.com/documents/{PROD_DOC}/v/{'9' * 24}/e/{EL}"
    assert [p for _, p, _ in fake.requests] == ["/api/companies", "/api/revisions/c/c0mpany/partnumber/PRT-000593"]


async def test_unknown_part_number(server):
    base, _ = server()
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-999999"})
    assert result.isError and "released" in _text(result)


@pytest.mark.parametrize("bad", ["../../documents/x", "PRT/1", "a" * 70, "-x"])
async def test_bad_part_number_rejected(server, bad):
    base, fake = server()
    result = await _call(base, "find_part_by_number", {"partNumber": bad})
    assert result.isError
    assert not any("/revisions/" in p for _, p, _ in fake.requests)


async def test_part_screenshot_by_number_has_working_image_link(server):
    base, fake = server()
    result = await _call(base, "capture_part_screenshot", {"partNumber": "PRT-000593", "view": "top"})
    assert not result.isError, _text(result)
    assert any(c.type == "image" for c in result.content)
    shaded = [p for _, p, _ in fake.requests if p.endswith("/shadedviews")]
    assert shaded == [f"/api/parts/d/{PROD_DOC}/v/{'9' * 24}/e/{EL}/partid/JHD/shadedviews"]
    m = re.search(r"!\[[^\]]*\]\((http://127\.0\.0\.1/img/[A-Za-z0-9_-]{32}\.png)\)", _text(result))
    assert m, _text(result)
    img_path = m.group(1).replace("http://127.0.0.1", "")
    resp = httpx.get(base + img_path)  # no token needed (capability URL)
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert resp.content.startswith(b"\x89PNG") and resp.headers["x-content-type-options"] == "nosniff"


async def test_upstream_screenshots_get_image_links_too(server):
    base, _ = server()
    args = {"documentId": PROD_DOC, "workspaceId": WS, "elementId": EL}
    result = await _call(base, "capture_part_studio_screenshot", args)
    assert "/img/" in _text(result)


@pytest.mark.parametrize("path", ["/img/../usage.json", "/img/short.png", "/img/" + "A" * 32 + ".png", "/img/x"])
def test_image_endpoint_rejects_unknown_names(server, path):
    base, _ = server()
    assert httpx.get(base + path).status_code in (401, 404)


def test_image_endpoint_post_requires_token(server):
    base, _ = server()
    assert httpx.post(base + "/img/" + "A" * 32 + ".png").status_code == 401


async def test_image_links_expire(server, tmp_path):
    import os

    base, _ = server()
    result = await _call(base, "capture_part_screenshot", {"partNumber": "PRT-000593"})
    url = re.search(r"(http://127\.0\.0\.1/img/\S+\.png)", _text(result)).group(1)
    name = url.rsplit("/", 1)[1]
    file = tmp_path / "images" / name
    old = file.stat().st_mtime - 25 * 3600
    os.utime(file, (old, old))
    assert httpx.get(base + "/img/" + name).status_code == 404
    assert not file.exists()


async def test_image_links_disabled(server):
    base, _ = server(image_links=False)
    result = await _call(base, "capture_part_screenshot", {"partNumber": "PRT-000593"})
    assert not result.isError and "/img/" not in _text(result)


# --- exports of released items & API-call efficiency ---------------------------------


def _link(text, ext):
    m = re.search(r"\((http://127\.0\.0\.1(/dl/[A-Za-z0-9_-]{32}/[^)]+\." + ext + r"))\)", text)
    assert m, text
    return m.group(2)


async def test_step_export_minimal_calls_and_cache(server):
    base, fake = server()
    result = await _call(base, "export_released_model", {"partNumber": "PRT-000593"})
    assert not result.isError, _text(result)
    calls = [(m, p) for m, p, _ in fake.requests]
    assert calls == [
        ("GET", "/api/companies"),
        ("GET", "/api/revisions/c/c0mpany/partnumber/PRT-000593"),
        ("POST", f"/api/partstudios/d/{PROD_DOC}/v/{'9' * 24}/e/{EL}/translations"),
        ("GET", "/api/translations/tr1"),
        ("GET", "/api/documents/d/" + PROD_DOC + "/externaldata/extstep"),
    ]
    post_body = fake.requests[2][2]
    assert post_body["formatName"] == "STEP" and post_body["partIds"] == "JHD" and post_body["storeInDocument"] is False
    resp = httpx.get(base + _link(_text(result), "step"))  # no token needed
    assert resp.status_code == 200 and resp.content.startswith(b"ISO-10303-21")
    assert 'attachment; filename="PRT-000593_RevA.step"' == resp.headers["content-disposition"]

    again = await _call(base, "export_released_model", {"partNumber": "PRT-000593"})
    assert "0 API calls" in _text(again)
    assert len(fake.requests) == 5  # nothing new


async def test_cache_survives_restart(server):
    base, fake = server()
    await _call(base, "export_released_model", {"partNumber": "PRT-000593"})
    base2, fake2 = server()  # new app instance, same volume
    result = await _call(base2, "export_released_model", {"partNumber": "PRT-000593"})
    assert not result.isError and fake2.requests == []


async def test_drawing_pdf_via_version_fallback(server):
    base, fake = server()
    fake.where_used_empty = True
    result = await _call(base, "export_released_drawing", {"partNumber": "PRT-000593"})
    assert not result.isError, _text(result)
    assert "not released" in _text(result)
    paths = [p for _, p, _ in fake.requests]
    assert f"/api/drawings/d/{PROD_DOC}/v/{'9' * 24}/e/{'7' * 24}/translations" in paths
    resp = httpx.get(base + _link(_text(result), "pdf"))
    assert resp.content.startswith(b"%PDF") and resp.headers["content-disposition"].startswith("inline")
    assert "content-security-policy" not in resp.headers  # PDF viewer must work


async def test_drawing_via_where_used(server):
    base, fake = server()
    result = await _call(base, "export_released_drawing", {"partNumber": "PRT-000900"})
    assert not result.isError, _text(result)
    assert "DRW-000635_RevB.pdf" in _text(result) and "not released" not in _text(result)
    calls = [(m, p) for m, p, _ in fake.requests]
    assert calls[2] == ("GET", "/api/productstructure/whereused")
    assert ("POST", f"/api/drawings/d/{LISTED_DOC}/v/{'2' * 24}/e/{'1' * 24}/translations") in calls  # Rev B
    assert not any(p.endswith("/elements") or "releasepackages" in p for _, p in calls)
    assert len(calls) == 8  # companies, revision, where used x2, drawing revisions, POST, poll, download
    again = await _call(base, "export_released_drawing", {"partNumber": "PRT-000900"})
    assert "0 API calls" in _text(again) and len(fake.requests) == 8


async def test_slow_export_resumes_without_new_translation(server):
    base, fake = server()
    fake.active_polls = 3  # longer than the first poll window (3 polls)
    first = await _call(base, "export_released_model", {"partNumber": "PRT-000593"})
    assert "still running" in _text(first)
    second = await _call(base, "export_released_model", {"partNumber": "PRT-000593"})
    assert not second.isError and ".step" in _text(second)
    assert sum(1 for m, p, _ in fake.requests if m == "POST") == 1


async def test_parallel_identical_exports_share_one_translation(server):
    import asyncio

    base, fake = server()
    results = await asyncio.gather(*[_call(base, "export_released_model", {"partNumber": "PRT-000593"}) for _ in range(3)])
    assert all(not r.isError for r in results)
    assert sum(1 for m, p, _ in fake.requests if m == "POST") == 1


async def test_released_screenshot_cached(server):
    base, fake = server()
    await _call(base, "capture_part_screenshot", {"partNumber": "PRT-000593"})
    n = len(fake.requests)
    again = await _call(base, "capture_part_screenshot", {"partNumber": "PRT-000593"})
    assert "0 API calls" in _text(again) and len(fake.requests) == n


@pytest.mark.parametrize("path", ["/dl/" + "A" * 32 + "/x.step", "/dl/../cache.json/x", "/dl/short/x.pdf"])
def test_download_endpoint_rejects_unknown(server, path):
    base, _ = server()
    assert httpx.get(base + path).status_code in (401, 404)


async def test_drawing_from_release_package(server):
    base, fake = server()
    fake.where_used_empty = True
    result = await _call(base, "export_released_drawing", {"partNumber": "PRT-000800"})
    assert not result.isError, _text(result)
    assert "not released" not in _text(result)
    paths = [p for _, p, _ in fake.requests]
    assert f"/api/drawings/d/{LISTED_DOC}/v/{'4' * 24}/e/{'3' * 24}/translations" in paths
    assert not any(p.endswith("/elements") for p in paths)


async def test_drawing_latest_revision_filter_saves_lookup(server):
    base, fake = server()
    fake.filter0_resolves = True
    result = await _call(base, "export_released_drawing", {"partNumber": "PRT-000900"})
    assert "DRW-000635_RevB.pdf" in _text(result), _text(result)
    calls = [p for _, p in [(m, p) for m, p, _ in fake.requests]]
    assert calls.count("/api/productstructure/whereused") == 1
    assert not any("/revisions/companies/" in p for p in calls)
    assert len(calls) == 6  # companies, revision, where used, POST, poll, download


# --- mapping table & release webhook ------------------------------------------------


async def test_mapping_table_and_drawing_lookup(server):
    base, fake = server()
    found = _json(await _call(base, "find_drawing_for_part", {"partNumber": "PRT-000900"}))
    assert found["drawings"][0]["drawingNumber"] == "DRW-000635" and found["drawings"][0]["revision"] == "B"
    assert found["drawings"][0]["onshapeUrl"].endswith(f"/documents/{LISTED_DOC}/v/{'2' * 24}/e/{'1' * 24}")
    assert found["partUrl"].startswith("https://cad.onshape.com/documents/")
    n = len(fake.requests)
    table = _json(await _call(base, "show_mapping_table", {"partNumber": "PRT-0009*"}))
    assert len(fake.requests) == n  # no API calls
    row = table["rows"][0]
    assert row["partNumber"] == "PRT-000900" and row["latest"] is True and row["drawings"] == ["DRW-000635 Rev B"]
    again = _json(await _call(base, "find_drawing_for_part", {"partNumber": "PRT-000900"}))
    assert again == found and len(fake.requests) == n


def test_webhook_admin_requires_token(server):
    base, fake = server()
    assert httpx.post(base + "/admin/webhook").status_code == 401
    assert httpx.get(base + "/admin/webhook").status_code == 401
    # a normal client token (e.g. ClickUp) must not reach the admin endpoints
    h = {"Authorization": f"Bearer {TOKEN}"}
    assert httpx.post(base + "/admin/webhook", headers=h).status_code == 403
    assert httpx.get(base + "/admin/webhook", headers=h).status_code == 403
    assert httpx.post(base + "/admin/sync?maxParts=200", headers=h).status_code == 403
    assert fake.requests == []


def test_admin_sync_cooldown_and_validation(server):
    base, fake = server()
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    assert httpx.post(base + "/admin/sync?maxParts=abc", headers=h).status_code == 400
    assert httpx.post(base + "/admin/sync", headers=h, timeout=30).status_code == 200
    n = len(fake.requests)
    second = httpx.post(base + "/admin/sync", headers=h)
    assert second.status_code == 429 and "cooldown" in second.json()["error"]
    assert len(fake.requests) == n


async def test_webhook_registration_and_invalidation(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    status = httpx.post(base + "/admin/webhook", headers=h).json()
    assert status["registered"] and status["active"]
    reg = [b for m, p, b in fake.requests if p == "/api/v6/webhooks"]
    assert len(reg) == 1 and reg[0]["isTransient"] is False and reg[0]["companyId"] == "c0mpany"
    assert "onshape.revision.created" in reg[0]["events"]
    secret = MappingStore(str(tmp_path / "mappings.db")).webhook_secret()
    assert reg[0]["url"] == f"http://127.0.0.1/onshape/webhook/{secret}"
    httpx.post(base + "/admin/webhook", headers=h)  # adds the metadata webhook once
    httpx.post(base + "/admin/webhook", headers=h)  # idempotent
    assert len([1 for _, p, _ in fake.requests if p == "/api/v6/webhooks"]) == 2

    # wrong secret is rejected, no token needed for the right one (Onshape can't send it)
    assert httpx.post(base + "/onshape/webhook/wrong", json={}).status_code == 404
    hook = base + f"/onshape/webhook/{secret}"
    assert httpx.post(hook, json={"event": "webhook.ping", "webhookId": "e" * 24}).status_code == 200

    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    n = len(fake.requests)
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert len(fake.requests) == n  # from the table
    event = {"event": "onshape.revision.created", "webhookId": "e" * 24, "partNumber": "PRT-000593", "elementType": 0}
    assert httpx.post(hook, json=event).status_code == 200
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert len(fake.requests) == n + 1  # re-checked exactly once after the release event


def test_mapping_ttl_depends_on_webhook(tmp_path):
    import time as _t

    from onshape_secure.mapping import MappingStore

    m = MappingStore(str(tmp_path / "m.db"))
    info = {"partNumber": "P-1", "revision": "A", "elementType": 0, "documentId": "a" * 24}
    m.put_revision("P-1", None, None, info)
    m._q("UPDATE latest SET checked_at=?", (_t.time() - 2 * 3600,))
    assert m.get_revision("P-1", None, None) is None  # no webhook: 1 h
    m.set_meta("webhook_id", "e" * 24)
    m.set_meta("webhook_verified_at", str(_t.time()))
    assert m.get_revision("P-1", None, None)["revision"] == "A"  # webhook: kept
    assert m.get_revision("P-1", "A", None)["revision"] == "A"  # fixed revision: always
    m.on_revision_created("P-1", 0)
    assert m.get_revision("P-1", None, None) is None
    assert m.get_revision("P-1", "A", None) is not None  # immutable facts survive


def test_big_files_expire_after_24h(tmp_path):
    import os
    import time as _t

    from onshape_secure.cache import FileCache

    fc = FileCache(str(tmp_path / "f"), 10**9, ttl_seconds=24 * 3600)
    path = fc.put("k", b"x" * 10)
    assert fc.get("k") == path
    old = _t.time() - 25 * 3600
    os.utime(path, (old, old))
    assert fc.get("k") is None and not os.path.exists(path)
    p2 = fc.put("k2", b"y")
    os.utime(p2, (old, old))
    fc._evict()  # periodic cleanup removes it even if never requested again
    assert not os.path.exists(p2)


# --- unreleased part numbers (workspace index) -------------------------------------------


async def test_unreleased_part_found_via_index_with_few_calls(server):
    base, fake = server()
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})  # loads company id
    n = len(fake.requests)
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert not result.isError, _text(result)
    info = _json(result)
    assert info["released"] is False and info["matches"][0]["name"] == "Anschluss Mitnehmer"
    assert info["matches"][0]["onshapeUrl"] == f"https://cad.onshape.com/documents/{PROD_DOC}/w/{WS}/e/{EL}"
    new = [p for _, p, _ in fake.requests[n:]]
    assert new == ["/api/revisions/c/c0mpany/partnumber/PRT-000583", "/api/v17/productstructure/whereused",
                   f"/api/v6/documents/{PROD_DOC}", f"/api/parts/d/{PROD_DOC}/w/{WS}"]
    m = len(fake.requests)
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert len(fake.requests) == m  # 0 calls: release miss + index hit come from the table
    shot = await _call(base, "capture_part_screenshot", {"partNumber": "PRT-000583"})
    assert not shot.isError and "NOT released" in _text(shot)
    assert any(p.endswith(f"/w/{WS}/e/{EL}/partid/JKD/shadedviews") for _, p, _ in fake.requests)


async def test_unknown_part_number_stops_without_browsing(server):
    base, fake = server()
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})  # where used -> indexes document
    n = len(fake.requests)
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-999999"})
    assert result.isError and "STOP" in _text(result) and "GEN3 Leg Assembly" in _text(result)
    assert [p for _, p, _ in fake.requests[n:]] == [
        "/api/revisions/c/c0mpany/partnumber/PRT-999999", "/api/v17/productstructure/whereused"]  # no browsing
    again = await _call(base, "find_part_by_number", {"partNumber": "PRT-999999"})
    assert again.isError and len(fake.requests) == n + 2  # misses are remembered
    assert fake.index_calls == 1


async def test_part_data_lives_only_in_mapping_db(server, tmp_path):
    """H-2/H-3: lookups, drawing links and where-used misses go to mappings.db; KV holds no part data."""
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000900"})
    await _call(base, "find_drawing_for_part", {"partNumber": "PRT-000900"})
    unreleased = _json(await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"}))
    assert "state" not in unreleased["matches"][0]  # Onshape said IN_PROGRESS: not a release status source
    assert all("state" not in h for h in _json(await _call(base, "search_part_index", {"text": "Mitnehmer"})))
    await _call(base, "find_part_by_number", {"partNumber": "PRT-999999"})
    kv_path = tmp_path / "cache.json"
    kv = json.loads(kv_path.read_text("utf-8")) if kv_path.exists() else {}
    assert not [k for k in kv if k.startswith(("rev2:", "whereused5:", "wu_pn_miss:"))], list(kv)
    m = MappingStore(str(tmp_path / "mappings.db"))
    assert m.whereused_miss("PRT-999999") and not m.whereused_miss("PRT-000583")
    assert m.get_drawings("PRT-000900", "A") is not None
    assert m.get_revision("PRT-000900", None, 0)["revision"] == "A"  # typed pointer from the untyped lookup
    assert m.meta("company_id") == "c0mpany"


async def test_metadata_webhook_marks_index_stale(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert fake.index_calls == 1
    secret = MappingStore(str(tmp_path / "mappings.db")).webhook_secret()
    httpx.post(base + f"/onshape/webhook/{secret}",
               json={"event": "onshape.model.lifecycle.metadata", "webhookId": "e" * 24, "documentId": PROD_DOC})
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert fake.index_calls == 2  # re-indexed once after the change


def test_webhook_ping_never_raises_trust(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    base, _ = server()
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    assert httpx.post(base + "/admin/webhook", headers=h).json()["active"]  # registration verifies
    m = MappingStore(str(tmp_path / "mappings.db"))
    stale = str(time.time() - 9 * 86400)
    m.set_meta("webhook_verified_at", stale)
    hook = base + f"/onshape/webhook/{m.webhook_secret()}"
    for name in ("webhook.ping", "webhook.register"):
        assert httpx.post(hook, json={"event": name, "webhookId": "e" * 24}).status_code == 200
    assert m.meta("webhook_verified_at") == stale
    assert httpx.get(base + "/admin/webhook", headers=h).json()["active"] is False


async def test_webhook_events_of_unknown_webhooks_ignored(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    m = MappingStore(str(tmp_path / "mappings.db"))
    hook = base + f"/onshape/webhook/{m.webhook_secret()}"
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    n = len(fake.requests)
    event = {"event": "onshape.revision.created", "partNumber": "PRT-000593", "elementType": 0}
    for wid in ({}, {"webhookId": "9" * 24}, {"webhookId": ["e" * 24]}, {"webhookId": None}):
        assert httpx.post(hook, json={**event, **wid}).status_code == 200
    assert m.meta("webhook_last_event_at") is None  # nothing written
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert len(fake.requests) == n  # still answered from the table


async def test_webhook_events_accepted_before_registration(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    n = len(fake.requests)
    hook = base + f"/onshape/webhook/{MappingStore(str(tmp_path / 'mappings.db')).webhook_secret()}"
    event = {"event": "onshape.revision.created", "partNumber": "PRT-000593", "elementType": 0}
    assert httpx.post(hook, json=event).status_code == 200
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert len(fake.requests) == n + 1  # invalidated, re-checked once
    for name in ("webhook.ping", "webhook.register"):
        httpx.post(hook, json={"event": name})
    assert MappingStore(str(tmp_path / "mappings.db")).meta("webhook_verified_at") is None  # stays empty


@pytest.mark.parametrize("name", ["onshape.revision.created", "onshape.workflow.transition"])
@pytest.mark.parametrize("document_id, reindexed", [(PROD_DOC, True), (None, False), ("Z" * 24, False)])
async def test_release_webhook_marks_document_stale(server, tmp_path, name, document_id, reindexed):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert fake.index_calls == 1
    event = {"event": name, "webhookId": "e" * 24, "partNumber": "PRT-000593", "elementType": 0}
    if document_id:
        event["documentId"] = document_id
    secret = MappingStore(str(tmp_path / "mappings.db")).webhook_secret()
    assert httpx.post(base + f"/onshape/webhook/{secret}", json=event).status_code == 200
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert fake.index_calls == (2 if reindexed else 1)


@pytest.mark.parametrize("payload, refetched", [
    ({"objectType": "RELEASE", "objectId": "5" * 24}, {"PRT-000800"}),  # known package: only its parts
    ({"objectType": "TASK", "objectId": "1" * 24}, set()),
    ({"objectType": "RELEASE", "objectId": "1" * 24}, set()),  # new release: revision.created follows
    ({"objectType": "OBSOLETION", "objectId": "1" * 24}, {"PRT-000800", "PRT-000593"}),
    ({}, {"PRT-000800", "PRT-000593"}),  # payload identifies nothing
    ({"objectType": "RELEASE", "objectId": "abc"}, {"PRT-000800", "PRT-000593"}),  # malformed id: unusable
    ({"objectType": "RELEASE", "objectId": 42}, {"PRT-000800", "PRT-000593"}),
    ({"objectType": "TASK", "objectId": "5" * 24}, set()),  # a task never touches release data
])
async def test_workflow_webhook_invalidates_only_affected_parts(server, tmp_path, payload, refetched):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    parts = ("PRT-000800", "PRT-000593")
    for pn in parts + ("PRT-000583",):  # the unreleased one indexes PROD_DOC
        await _call(base, "find_part_by_number", {"partNumber": pn})
    assert fake.index_calls == 1
    hook = base + f"/onshape/webhook/{MappingStore(str(tmp_path / 'mappings.db')).webhook_secret()}"
    event = {"event": "onshape.workflow.transition", "webhookId": "e" * 24, **payload}
    assert httpx.post(hook, json=event).status_code == 200
    for pn in parts:
        n = len(fake.requests)
        await _call(base, "find_part_by_number", {"partNumber": pn})
        assert (len(fake.requests) > n) == (pn in refetched), pn
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert fake.index_calls == (2 if refetched == {"PRT-000800"} else 1)  # the known package's document is stale


def test_workflow_webhook_log_shows_type_and_transition(server, tmp_path, capfd):
    from onshape_secure.mapping import MappingStore

    base, _ = server(log_level="INFO")
    hook = base + f"/onshape/webhook/{MappingStore(str(tmp_path / 'mappings.db')).webhook_secret()}"
    httpx.post(hook, json={"event": "onshape.workflow.transition", "objectType": "RELEASE", "objectId": "1" * 24,
                           "transitionName": "SUBMIT\n2026-01-01 AUDIT forged"})
    out = "".join(capfd.readouterr())
    assert 'object="RELEASE" transition="SUBMIT\\n2026-01-01 AUDIT forged" invalidated=none' in out
    assert "\n2026-01-01 AUDIT forged" not in out


@pytest.mark.parametrize("extra, reindexed", [
    ({"documentId": "Z" * 24}, False), ({"documentId": "abc"}, False), ({"documentId": 42}, False),
    ({}, True), ({"documentId": None}, True), ({"documentId": ""}, True),
])
async def test_metadata_webhook_invalid_document_marks_nothing(server, tmp_path, extra, reindexed):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    secret = MappingStore(str(tmp_path / "mappings.db")).webhook_secret()
    event = {"event": "onshape.model.lifecycle.metadata", "webhookId": "e" * 24, **extra}
    assert httpx.post(base + f"/onshape/webhook/{secret}", json=event).status_code == 200
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert fake.index_calls == (2 if reindexed else 1)  # no id: all documents (unchanged); malformed: none


async def test_metadata_webhook_id_is_known(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    httpx.post(base + "/admin/webhook", headers=h)
    m = MappingStore(str(tmp_path / "mappings.db"))
    meta_id = "7" * 24  # the fake hands out one id for every webhook
    m.set_meta("webhook_meta_id", meta_id)
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    httpx.post(base + f"/onshape/webhook/{m.webhook_secret()}",
               json={"event": "onshape.model.lifecycle.metadata", "webhookId": meta_id, "documentId": PROD_DOC})
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert fake.index_calls == 2


def test_webhook_log_is_escaped(server, tmp_path, capfd):
    from onshape_secure.mapping import MappingStore

    base, _ = server(log_level="INFO")
    hook = base + f"/onshape/webhook/{MappingStore(str(tmp_path / 'mappings.db')).webhook_secret()}"
    forged = {"event": "x\n2026-01-01 AUDIT forged", "partNumber": "P\n2026-01-01 AUDIT forged"}
    httpx.post(hook, json=forged)  # accepted path (not registered yet): INFO line
    httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    httpx.post(hook, json={**forged, "webhookId": "9" * 24})  # unknown webhook: WARNING line
    out = "".join(capfd.readouterr())
    assert "webhook event=" in out and "unknown webhook ignored" in out
    assert "\n2026-01-01 AUDIT forged" not in out


async def test_document_hint_indexes_named_document(server):
    base, fake = server()
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-000583", "documentId": PROD_DOC})
    assert not result.isError and _json(result)["released"] is False


async def test_get_parts_shows_part_numbers(server):
    base, _ = server()
    result = await _call(base, "get_parts", {"documentId": PROD_DOC, "workspaceId": WS, "elementId": EL})
    assert _json(result)[0]["partNumber"] == "PRT-000593"


async def test_metadata_webhook_is_separate_and_optional(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    httpx.post(base + "/admin/webhook", headers=h)  # creates release webhook
    status = httpx.post(base + "/admin/webhook", headers=h).json()  # then the metadata one
    created = [b for m, p, b in fake.requests if p == "/api/v6/webhooks"]
    assert [c["events"] for c in created] == [
        ["onshape.revision.created", "onshape.workflow.transition"], ["onshape.model.lifecycle.metadata"]]
    assert status["metadataEvents"] is True
    httpx.post(base + "/admin/webhook", headers=h)
    assert len([1 for _, p, _ in fake.requests if p == "/api/v6/webhooks"]) == 2  # idempotent


async def test_search_part_index_is_free(server):
    base, fake = server()
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})  # builds the index
    n = len(fake.requests)
    result = await _call(base, "search_part_index", {"text": "Mitnehmer"})
    names = sorted(r["name"] for r in _json(result))
    assert names == ["Anschluss Mitnehmer", "Anschluss Mitnehmer spiegel"] and len(fake.requests) == n
    assert "[Onshape-API: 0 Aufruf(e)" in _text(result)


async def test_every_result_reports_onshape_cost(server):
    base, fake = server()
    first = await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert "[Onshape-API: 2 Aufruf(e) für diese Anfrage · heute 2/" in _text(first)  # companies + revision
    again = await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert "[Onshape-API: 0 Aufruf(e)" in _text(again)
    missing = await _call(base, "get_document", {"documentId": "9" * 24})  # 404: not billed
    assert missing.isError and "[Onshape-API: 0 Aufruf(e)" in _text(missing)


# --- batch sync ----------------------------------------------------------------------------


def test_batch_sync_builds_table_cheaply(server, monkeypatch):
    import onshape_secure.app as app_mod

    monkeypatch.setattr(app_mod, "ADMIN_SYNC_COOLDOWN", 0)
    base, fake = server()
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    assert httpx.post(base + "/admin/sync").status_code == 401
    res = httpx.post(base + "/admin/sync?document=Robot%20Gen3&maxParts=10", headers=h, timeout=30).json()
    assert res["revisions"] == {"pages": 2, "revisionsStored": 2, "complete": True, "full": False}
    assert res["drawings"]["partsWithDrawing"] == 1
    paths = [p for _, p, _ in fake.requests]
    assert not any("/revisions/companies/c0mpany/d/" in p for p in paths)  # drawing revision came from the sync
    assert res["onshapeCalls"] == len(fake.requests)  # companies + 2 pages + where used x2
    assert res["onshapeCalls"] == 5
    n = len(fake.requests)
    again = httpx.post(base + "/admin/sync?document=Robot%20Gen3&maxParts=10", headers=h, timeout=30).json()
    assert again["onshapeCalls"] == 1 and fake.sync_pages[-1]["after"].startswith("2026-03")  # incremental
    assert len(fake.requests) == n + 1


async def test_synced_table_answers_without_calls(server):
    base, fake = server()
    httpx.post(base + "/admin/sync?document=Robot%20Gen3&maxParts=10",
               headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=30)
    n = len(fake.requests)
    found = _json(await _call(base, "find_drawing_for_part", {"partNumber": "PRT-000900"}))
    assert found["drawings"][0]["drawingNumber"] == "DRW-000635" and found["drawings"][0]["revision"] == "B"
    drw = _json(await _call(base, "find_part_by_number", {"partNumber": "DRW-000635"}))
    assert drw["revision"] == "B"
    assert len(fake.requests) == n  # both answered from the table


async def test_export_mapping_table_csv(server):
    base, fake = server()
    httpx.post(base + "/admin/sync?document=Robot%20Gen3&maxParts=10",
               headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=30)
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})  # unreleased, indexed
    n = len(fake.requests)
    result = await _call(base, "export_mapping_table", {})
    assert len(fake.requests) == n and "[Onshape-API: 0 Aufruf(e)" in _text(result)
    path = re.search(r"\(http://127\.0\.0\.1(/dl/[^)]+\.csv)\)", _text(result)).group(1)
    csv_text = httpx.get(base + path).content.decode("utf-8-sig")
    lines = csv_text.splitlines()
    assert lines[0].startswith("Teilenummer;Revision;Typ;Status")
    assert any(l.startswith("PRT-000900;A;Teil;freigegeben") and "DRW-000635 Rev B" in l for l in lines)
    assert any(l.startswith("PRT-000583;") and "nicht freigegeben" in l for l in lines)


# --- H-9: read scope applies to every document an own tool reads (AD-14) ----

SCOPE_ONLY = dict(write_mode="off", write_allowed_document_ids=frozenset())  # no write-based read grants


async def test_read_scope_hides_drawings_of_other_documents(server):
    base, fake = server(read_allowed_document_ids=frozenset({PROD_DOC}), **SCOPE_ONLY)
    fake.filter0_resolves = True  # the drawing lives in LISTED_DOC
    found = _json(await _call(base, "find_drawing_for_part", {"partNumber": "PRT-000900"}))
    assert found["partUrl"] and found["drawings"] == []
    # where-used (PRT-000900) and release package (PRT-000800) point to LISTED_DOC: both dropped,
    # the export falls back to the drawing in the part's own document instead of failing
    for pn in ("PRT-000900", "PRT-000800"):
        result = await _call(base, "export_released_drawing", {"partNumber": pn})
        assert not result.isError and "Read access" not in _text(result), _text(result)
    assert not any(LISTED_DOC in p for m, p, _ in fake.requests if m == "POST")


async def test_read_scope_drawing_lookup_unchanged_inside_scope(server):
    base, fake = server(read_allowed_document_ids=frozenset({PROD_DOC, LISTED_DOC}), **SCOPE_ONLY)
    fake.filter0_resolves = True
    found = _json(await _call(base, "find_drawing_for_part", {"partNumber": "PRT-000900"}))
    assert [d["drawingNumber"] for d in found["drawings"]] == ["DRW-000635"]


async def test_read_scope_unreleased_lookup_skips_other_documents(server):
    base, fake = server()
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})  # indexes PROD_DOC (no scope)
    assert fake.index_calls == 1
    base, fake = server(read_allowed_document_ids=frozenset({LISTED_DOC}), **SCOPE_ONLY)
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})
    assert result.isError and "STOP" in _text(result)
    assert "Anschluss" not in _text(result) and "GEN3 Leg Assembly" not in _text(result)  # no hit, no doc name
    shot = await _call(base, "capture_part_screenshot", {"partNumber": "PRT-000583"})
    assert shot.isError and not any(p.endswith("/shadedviews") for _, p, _ in fake.requests)
    hinted = await _call(base, "find_part_by_number", {"partNumber": "PRT-000583", "documentId": PROD_DOC})
    assert hinted.isError and "Read access" in _text(hinted)


async def test_read_scope_never_indexes_other_documents(server):
    base, fake = server(read_allowed_document_ids=frozenset({LISTED_DOC}), **SCOPE_ONLY)
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})  # where used -> PROD_DOC
    assert result.isError and fake.index_calls == 0
    assert "STOP" in _text(result) and "Read access" not in _text(result)  # skipped, not aborted
    assert not any(p == f"/api/v6/documents/{PROD_DOC}" for _, p, _ in fake.requests)


async def test_read_scope_skips_stale_documents_outside_scope(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    base, fake = server()
    await _call(base, "find_part_by_number", {"partNumber": "PRT-000583"})  # indexes PROD_DOC (no scope)
    MappingStore(str(tmp_path / "mappings.db")).mark_doc_dirty(PROD_DOC)  # stale -> re-index candidate
    base, fake = server(read_allowed_document_ids=frozenset({LISTED_DOC}), **SCOPE_ONLY)
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-000584"})
    assert result.isError and "STOP" in _text(result) and "Read access" not in _text(result)
    assert fake.index_calls == 0


# --- H-5/H-6: one call scope, JSON audit everywhere, billed calls, persisted sync cooldown ---


def _audits(out: str) -> list[dict]:
    lines = [line for line in out.splitlines() if "AUDIT" in line]
    return [json.loads(line.split("AUDIT ", 1)[1]) for line in lines]  # every AUDIT line is JSON


def _usage_calls(result) -> int:
    return int(re.search(r"\[Onshape-API: (\d+) Aufruf", _text(result)).group(1))


def _day_count(tmp_path) -> int:
    return json.loads((tmp_path / "usage.json").read_text())["day_count"]


async def test_document_name_lookup_is_guarded_and_metered(server, tmp_path, capfd):
    base, fake = server(log_level="INFO")
    args = {"documentId": AI_DOC, "workspaceId": WS, "elementId": EL, "featureId": "Fabc_1"}
    result = await _call(base, "delete_feature", args)  # check_write looks up the document name
    assert not result.isError, _text(result)
    assert ("GET", f"/api/v6/documents/{AI_DOC}", None) in fake.requests
    audit = [a for a in _audits(capfd.readouterr().err) if a["action"] == "delete_feature"][0]
    assert audit["event"] == "tool_call" and audit["decision"] == "allowed"
    assert audit["onshapeCalls"] == audit["attempts"] == len(fake.requests) == _usage_calls(result)
    assert _day_count(tmp_path) == len(fake.requests)


async def test_failed_request_is_an_attempt_not_a_billed_call(server, tmp_path, capfd):
    base, fake = server(log_level="INFO")
    result = await _call(base, "get_document", {"documentId": "9" * 24})  # Onshape answers 404
    assert len(fake.requests) == 1
    audit = [a for a in _audits(capfd.readouterr().err) if a["action"] == "get_document"][0]
    assert audit["attempts"] == 1 and audit["onshapeCalls"] == 0 == _usage_calls(result)
    assert _day_count(tmp_path) == 0  # refunded


def test_admin_actions_write_json_audit_with_billed_calls(server, capfd):
    base, fake = server(log_level="INFO")
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    assert httpx.post(base + "/admin/sync", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 403
    hook = httpx.post(base + "/admin/webhook", headers=admin).json()
    assert hook["onshapeCalls"] == len(fake.requests) == 2 and "onshapeRequests" not in hook  # companies + POST
    n = len(fake.requests)
    sync = httpx.post(base + "/admin/sync", headers=admin, timeout=30).json()
    assert sync["onshapeCalls"] == len(fake.requests) - n
    assert httpx.post(base + "/admin/sync", headers=admin).status_code == 429
    audits = [a for a in _audits(capfd.readouterr().err) if a["event"] == "admin"]
    seen = [(a["action"], a["decision"], a.get("reason"), a["client"]) for a in audits]
    assert seen == [
        ("admin_sync", "denied", "admin token required", "clickup"),
        ("admin_webhook", "allowed", None, "admin"),
        ("admin_sync", "allowed", None, "admin"),
        ("admin_sync", "denied", "cooldown", "admin"),
    ]
    assert audits[1]["onshapeCalls"] == 2 and audits[1]["cls"] == "system"
    assert audits[2]["onshapeCalls"] == sync["onshapeCalls"] and audits[3]["onshapeCalls"] == 0


def test_sync_cooldown_survives_restart(server):
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    base, _ = server()
    assert httpx.post(base + "/admin/sync", headers=admin, timeout=30).status_code == 200
    base, fake = server()  # new process state, same data directory (auto-stop restart)
    second = httpx.post(base + "/admin/sync", headers=admin)
    assert second.status_code == 429 and "cooldown" in second.json()["error"]
    assert fake.requests == []


def test_sync_cooldown_ignores_start_time_in_the_future(server, tmp_path):
    from onshape_secure.mapping import MappingStore

    MappingStore(str(tmp_path / "mappings.db")).set_meta("admin_sync_last", str(time.time() + 86400))
    base, _ = server()
    assert httpx.post(base + "/admin/sync", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                      timeout=30).status_code == 200


async def test_webhook_check_job_has_own_audit_and_shows_in_usage_line(server, tmp_path, capfd):
    from onshape_secure.mapping import MappingStore

    base, fake = server(log_level="INFO")
    httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    MappingStore(str(tmp_path / "mappings.db")).set_meta("webhook_verified_at", str(time.time() - 8 * 86400))
    n = len(fake.requests)
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert ("GET", "/api/v6/webhooks/" + "e" * 24, None) in fake.requests[n:]
    audits = _audits(capfd.readouterr().err)
    job = [a for a in audits if a["event"] == "job"]
    tool = [a for a in audits if a["action"] == "find_part_by_number"][0]
    assert len(job) == 1 and job[0]["action"] == "verify_webhook" and job[0]["onshapeCalls"] == 1
    assert tool["onshapeCalls"] + 1 == len(fake.requests) - n == _usage_calls(result)


def _intercept(fake, rule):
    """Route Onshape traffic through `rule(request)` first; None falls through to the fake."""
    from onshape_mcp import server as upstream

    def handler(request):
        answer = rule(request)
        if answer is None:
            return fake(request)
        fake.requests.append((request.method, request.url.path, None))
        if isinstance(answer, Exception):
            raise answer
        return answer

    upstream.client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_export_download_is_metered(server, tmp_path, capfd):
    base, fake = server(log_level="INFO")
    result = await _call(base, "export_released_model", {"partNumber": "PRT-000593"})
    assert not result.isError, _text(result)
    assert fake.requests[-1][1].endswith("/externaldata/extstep")  # the get_bytes download
    audit = [a for a in _audits(capfd.readouterr().err) if a["action"] == "export_released_model"][0]
    assert audit["onshapeCalls"] == audit["attempts"] == len(fake.requests) == _usage_calls(result) == 5
    assert _day_count(tmp_path) == 5


async def test_failed_download_and_transport_error_are_refunded(server, tmp_path, capfd):
    base, fake = server(log_level="INFO")
    _intercept(fake, lambda r: httpx.Response(404) if "/externaldata/" in r.url.path else None)
    failed = await _call(base, "export_released_model", {"partNumber": "PRT-000593"})
    assert failed.isError
    _intercept(fake, lambda r: httpx.ConnectError("down", request=r))
    down = await _call(base, "get_document", {"documentId": PROD_DOC})
    assert down.isError
    audits = {a["action"]: a for a in _audits(capfd.readouterr().err)}
    export, doc = audits["export_released_model"], audits["get_document"]
    assert export["attempts"] == 5 and export["onshapeCalls"] == 4 == _usage_calls(failed)
    assert doc["attempts"] == 1 and doc["onshapeCalls"] == 0 == _usage_calls(down)
    assert _day_count(tmp_path) == 4


async def test_vanished_webhook_job_audits_error_and_falls_back(server, tmp_path, capfd):
    from onshape_secure.mapping import MappingStore

    base, fake = server(log_level="INFO")
    httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    m = MappingStore(str(tmp_path / "mappings.db"))
    m.set_meta("webhook_verified_at", str(time.time() - 8 * 86400))
    _intercept(fake, lambda r: httpx.Response(404) if r.url.path == "/api/v6/webhooks/" + "e" * 24 else None)
    result = await _call(base, "find_part_by_number", {"partNumber": "PRT-000593"})
    assert not result.isError, _text(result)  # the job never fails the tool call
    job = [a for a in _audits(capfd.readouterr().err) if a["event"] == "job"]
    assert len(job) == 1 and job[0]["decision"] == "error" and job[0]["onshapeStatus"] == 404
    assert job[0]["attempts"] == 1 and job[0]["onshapeCalls"] == 0
    assert m.meta("webhook_id") is None  # timed re-checks again


@pytest.mark.parametrize("answer, error", [
    (httpx.Response(200, json={"id": "not-an-id"}), "Onshape did not return a webhook id"),
    (httpx.Response(500, json={}), "registration failed"),
])
def test_webhook_registration_failures_are_audited(server, capfd, answer, error):
    base, fake = server(log_level="INFO")
    _intercept(fake, lambda r: answer if r.url.path == "/api/v6/webhooks" else None)
    resp = httpx.post(base + "/admin/webhook", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
    assert resp.status_code == 502 and resp.json()["error"] == error
    audit = [a for a in _audits(capfd.readouterr().err) if a["action"] == "admin_webhook"][0]
    assert audit["decision"] == "error"


@pytest.mark.parametrize("overrides, rule, status, decision", [
    ({"onshape_daily_limit": 1}, lambda r: None, 429, "denied"),  # budget runs out mid-sync
    ({}, lambda r: httpx.Response(500) if r.url.path.startswith("/api/revisions/") else None, 502, "error"),
])
def test_admin_sync_failures_are_audited(server, capfd, overrides, rule, status, decision):
    base, fake = server(log_level="INFO", **overrides)
    _intercept(fake, rule)
    resp = httpx.post(base + "/admin/sync", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}, timeout=30)
    assert resp.status_code == status and "onshapeCalls" in resp.json()
    audit = [a for a in _audits(capfd.readouterr().err) if a["action"] == "admin_sync"][0]
    assert audit["decision"] == decision and audit["onshapeCalls"] == resp.json()["onshapeCalls"]


def test_admin_sync_bad_requests_are_audited(server, capfd):
    base, fake = server(log_level="INFO")
    h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    assert httpx.post(base + "/admin/sync?maxParts=abc", headers=h).status_code == 400
    assert httpx.post(base + "/admin/sync?maxParts=5&document=Nope", headers=h, timeout=30).status_code == 400
    reasons = [a.get("reason") for a in _audits(capfd.readouterr().err) if a["action"] == "admin_sync"]
    assert reasons == ["bad maxParts", "unknown document"]


def test_full_sync_refills_latest_pointers(server, tmp_path):
    from onshape_secure.app import SYNC_COOLDOWN_KEY
    from onshape_secure.mapping import MappingStore

    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    base, fake = server()
    m = MappingStore(str(tmp_path / "mappings.db"))

    def sync(query=""):
        m.set_meta(SYNC_COOLDOWN_KEY, "0")  # skip the cooldown between runs
        return httpx.post(base + "/admin/sync" + query, headers=admin, timeout=30).json()

    pointers = sync()["table"]["latest_pointers"]
    assert pointers == 4  # typed + untyped pointer per revision
    cursor = m.meta("rev_sync_after")
    m.invalidate_all()  # e.g. an obsoletion event
    again = sync("?full=0")
    assert again["table"]["latest_pointers"] == 0 and fake.sync_pages[-1]["after"] == cursor  # only new ones
    assert again["revisions"]["full"] is False
    full = sync("?full=1")
    assert full["table"]["latest_pointers"] == pointers and full["revisions"]["complete"] and full["revisions"]["full"]
    assert fake.sync_pages[-2]["after"] == "2000-01-01T00:00:00Z"  # page 1 of the full run, then page 2
    assert full["onshapeCalls"] == 2 and m.meta("rev_sync_after") == cursor  # cursor never moves backwards


@pytest.mark.parametrize("value", ["true", "2", "abc"])
def test_full_sync_rejects_bad_flag(server, value):
    base, fake = server()
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    resp = httpx.post(base + f"/admin/sync?full={value}", headers=admin, timeout=30)
    assert resp.status_code == 400 and fake.requests == []
    assert httpx.post(base + "/admin/sync", headers=admin, timeout=30).status_code == 200  # no cooldown spent
