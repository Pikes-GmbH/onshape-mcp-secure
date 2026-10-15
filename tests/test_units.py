import asyncio
import json

import pytest

from conftest import AI_DOC, LISTED_DOC, PROD_DOC, make_settings
from onshape_secure import config as cfg
from onshape_secure.policy import (
    CREATE_DOC,
    DELETE,
    READ,
    WRITE,
    TOOL_CLASSES,
    check_onshape_request,
    DocumentAccess,
    PolicyDenied,
    is_tool_enabled,
    sanitize_arguments,
)
from onshape_secure.quota import OnshapeBudget, QuotaExceeded, RateLimiter


async def test_every_upstream_tool_is_classified():
    from onshape_mcp import server as upstream

    from onshape_secure.policy import EXTRA_TOOL_NAMES

    names = {t.name for t in await upstream.list_tools()}
    assert names | EXTRA_TOOL_NAMES == set(TOOL_CLASSES), (names | EXTRA_TOOL_NAMES) ^ set(TOOL_CLASSES)


def test_unknown_tool_not_enabled(settings):
    assert not is_tool_enabled("some_new_upstream_tool", settings)


SCHEMA = {"properties": {k: {} for k in [
    "documentId", "name", "edgeIds", "radius", "script", "featureId", "elementType", "view",
    "outputWidth", "limit"]}}


@pytest.mark.parametrize("args", [
    {"edgeIds": ["ok", "../x"]},
    {"edgeIds": ["x"] * 201},
    {"radius": float("inf")},
    {"radius": 1e12},
    {"name": "x" * 501},
    {"script": "x" * 20_001},
    {"name": {"nested": 1}},
    {"name": "a\x00b"},
])
def test_bad_arguments_rejected(settings, args):
    with pytest.raises(PolicyDenied):
        sanitize_arguments("create_fillet", args, SCHEMA, settings)


def test_unknown_arguments_dropped(settings):
    clean = sanitize_arguments("get_document", {"documentId": PROD_DOC, "outputPath": "/etc/x"}, SCHEMA, settings)
    assert clean == {"documentId": PROD_DOC}


@pytest.mark.parametrize("fid", ["a/b", "a.b", "a%2Fb", "a?b", ""])
def test_delete_feature_id_must_be_path_safe(settings, fid):
    with pytest.raises(PolicyDenied):
        sanitize_arguments("delete_feature", {"featureId": fid}, SCHEMA, settings)


def test_screenshot_view_validation(settings):
    sanitize_arguments("capture_part_studio_screenshot", {"view": "ISO"}, SCHEMA, settings)
    sanitize_arguments("capture_part_studio_screenshot", {"view": ",".join(["0.5"] * 12)}, SCHEMA, settings)
    with pytest.raises(PolicyDenied):
        sanitize_arguments("capture_part_studio_screenshot", {"view": "top&x=1"}, SCHEMA, settings)


def test_limit_clamped(settings):
    assert sanitize_arguments("list_documents", {"limit": 10_000}, SCHEMA, settings)["limit"] == 50


async def test_document_access(tmp_path):
    names = {AI_DOC: "AI-x", PROD_DOC: "Prod"}

    async def fetch(doc):
        if doc not in names:
            raise RuntimeError("404")
        return names[doc]

    access = DocumentAccess(make_settings(tmp_path), fetch)
    await access.check_write(AI_DOC)
    await access.check_write(LISTED_DOC)
    for doc in (PROD_DOC, "9" * 24, None):
        with pytest.raises(PolicyDenied):
            await access.check_write(doc)

    off = DocumentAccess(make_settings(tmp_path, write_mode="off"), fetch)
    with pytest.raises(PolicyDenied):
        await off.check_write(LISTED_DOC)

    no_prefix = DocumentAccess(make_settings(tmp_path, write_allowed_name_prefix=""), fetch)
    with pytest.raises(PolicyDenied):
        await no_prefix.check_write(AI_DOC)


async def test_budget_persists_and_limits(tmp_path):
    path = str(tmp_path / "u.json")
    budget = OnshapeBudget(path, daily_limit=3, monthly_limit=100)
    for _ in range(3):
        await budget.check()
        await budget.reserve()
    with pytest.raises(QuotaExceeded):
        await budget.reserve()
    reloaded = OnshapeBudget(path, daily_limit=3, monthly_limit=100)
    with pytest.raises(QuotaExceeded):
        await reloaded.check()
    assert json.load(open(path))["month_count"] == 3


async def test_monthly_budget(tmp_path):
    budget = OnshapeBudget(str(tmp_path / "u.json"), daily_limit=0, monthly_limit=1)
    await budget.reserve()
    with pytest.raises(QuotaExceeded):
        await budget.check()


def test_rate_limiter():
    limiter = RateLimiter(2)
    limiter.hit("a"), limiter.hit("a"), limiter.hit("b")
    with pytest.raises(QuotaExceeded):
        limiter.hit("a")


# --- configuration -----------------------------------------------------------

GOOD_ENV = {
    "ONSHAPE_ACCESS_KEY": "ak",
    "ONSHAPE_SECRET_KEY": "sk",
    "MCP_AUTH_TOKENS": "clickup:" + "x" * 40,
}


def _load(monkeypatch, **env):
    for key in list(cfg.os.environ):
        if key in {"MCP_AUTH_TOKENS", "WRITE_MODE", "ONSHAPE_BASE_URL", "WRITE_ALLOWED_DOCUMENT_IDS",
                   "DISABLED_TOOLS", "WRITE_ALLOWED_NAME_PREFIX"}:
            monkeypatch.delenv(key, raising=False)
    for key, value in {**GOOD_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    return cfg.load_settings()


def test_config_defaults(monkeypatch):
    s = _load(monkeypatch)
    assert s.write_mode == "allowlist" and s.write_allowed_name_prefix == "AI-"
    assert s.auth_tokens == {"x" * 40: "clickup"}
    assert "export_part_studio" in s.disabled_tools


@pytest.mark.parametrize("env", [
    {"MCP_AUTH_TOKENS": ""},
    {"MCP_AUTH_TOKENS": "short"},
    {"MCP_AUTH_TOKENS": "bad label!:" + "x" * 40},
    {"WRITE_MODE": "yolo"},
    {"ONSHAPE_BASE_URL": "http://cad.onshape.com"},
    {"ONSHAPE_BASE_URL": "https://evil.example.com"},
    {"ONSHAPE_BASE_URL": "https://onshape.com.evil.io"},
    {"ONSHAPE_BASE_URL": "https://cad.onshape.com/api"},
    {"WRITE_ALLOWED_DOCUMENT_IDS": "not-an-id"},
    {"WRITE_ALLOWED_NAME_PREFIX": "A"},
    {"ONSHAPE_ACCESS_KEY": ""},
])
def test_config_rejects_unsafe(monkeypatch, env):
    with pytest.raises(cfg.ConfigError):
        _load(monkeypatch, **env)


def test_enterprise_base_url(monkeypatch):
    assert _load(monkeypatch, ONSHAPE_BASE_URL="https://pikes.onshape.com/").onshape_base_url == "https://pikes.onshape.com"


async def test_budget_reservation_is_atomic(tmp_path):
    budget = OnshapeBudget(str(tmp_path / "u.json"), daily_limit=5, monthly_limit=0)
    results = await asyncio.gather(*[budget.reserve() for _ in range(20)], return_exceptions=True)
    assert sum(r is None for r in results) == 5


# --- HTTP-layer guard ----------------------------------------------------------

P = f"/api/v9/partstudios/d/{AI_DOC}/w/{'d' * 24}/e/{'e' * 24}/features"


@pytest.mark.parametrize("method,path,cls,doc", [
    ("get", P, READ, None),
    ("post", P, WRITE, AI_DOC),
    ("delete", P + "/featureid/F1", DELETE, AI_DOC),
    ("post", f"/api/v8/partstudios/d/{PROD_DOC}/w/x/e/y/featurescript", READ, None),
    ("post", "/api/v10/documents", CREATE_DOC, None),
    ("post", "/api/webhooks", "system", None),
])
def test_guard_allows(method, path, cls, doc):
    check_onshape_request(method, path, cls, doc)


@pytest.mark.parametrize("method,path,cls,doc", [
    ("post", P, READ, PROD_DOC),             # read tool writing (face CS case)
    ("delete", P, READ, AI_DOC),
    ("post", P, WRITE, PROD_DOC),            # path doc != gated doc
    ("post", P.replace(AI_DOC, PROD_DOC), WRITE, AI_DOC),
    ("post", "/api/v10/documents", WRITE, AI_DOC),
    ("post", P, None, None),                 # outside any tool call
    ("delete", f"/api/v6/documents/{AI_DOC}", DELETE, AI_DOC),  # no /d/ segment -> blocked
    ("post", f"/api/v9/assemblies/d/{AI_DOC}/w/x/e/y/features?x=/d/{PROD_DOC}/", WRITE, PROD_DOC),
    ("post", "/api/webhooks", READ, None),      # tools can never register webhooks
    ("post", "/api/webhooks", WRITE, AI_DOC),
])
def test_guard_blocks(method, path, cls, doc):
    with pytest.raises(PolicyDenied):
        check_onshape_request(method, path, cls, doc)


async def test_websocket_scope_rejected():
    from onshape_secure.auth import BearerAuthMiddleware

    reached, sent = [], []

    async def inner(scope, receive, send):
        reached.append(scope)

    async def send(msg):
        sent.append(msg)

    mw = BearerAuthMiddleware(inner, {"x" * 40: "a"})
    await mw({"type": "websocket", "path": "/messages/", "headers": []}, None, send)
    assert reached == [] and sent[0]["type"] == "websocket.close"


def test_screenshot_view_defaults_to_auto(settings):
    from onshape_secure.policy import AUTO_VIEW_MATRIX

    assert sanitize_arguments("capture_part_studio_screenshot", {}, SCHEMA, settings)["view"] == AUTO_VIEW_MATRIX
    assert sanitize_arguments("capture_part_screenshot", {"view": "auto"}, SCHEMA, settings)["view"] == AUTO_VIEW_MATRIX
    assert sanitize_arguments("capture_part_screenshot", {"view": "top"}, SCHEMA, settings)["view"] == "top"


async def test_budget_refund_and_yearly_and_attempt_cap(tmp_path, monkeypatch):
    import onshape_secure.quota as q

    budget = OnshapeBudget(str(tmp_path / "u.json"), daily_limit=0, monthly_limit=0, yearly_limit=2)
    await budget.reserve()
    await budget.refund()
    await budget.reserve()
    await budget.reserve()
    with pytest.raises(QuotaExceeded):
        await budget.reserve()  # yearly limit
    monkeypatch.setattr(q, "ATTEMPTS_PER_DAY", 3)
    b2 = OnshapeBudget(str(tmp_path / "v.json"), daily_limit=0, monthly_limit=0)
    for _ in range(3):
        await b2.reserve()
        await b2.refund()
    with pytest.raises(QuotaExceeded):
        await b2.reserve()  # failed calls are free but capped


async def test_weekly_limit(tmp_path):
    budget = OnshapeBudget(str(tmp_path / "w.json"), daily_limit=0, monthly_limit=0, weekly_limit=2)
    await budget.reserve()
    await budget.reserve()
    with pytest.raises(QuotaExceeded):
        await budget.reserve()
    assert budget.snapshot()["this_week_utc"] == 2
