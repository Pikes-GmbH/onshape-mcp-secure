import asyncio
import os
import inspect
import json
import re

import pytest

from conftest import AI_DOC, LISTED_DOC, PROD_DOC, make_settings
from onshape_secure import config as cfg
from onshape_secure.policy import (
    CREATE_DOC,
    DELETE,
    INDEX,
    META,
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


# Every env var load_settings() reads, so default tests do not depend on the shell.
CONFIG_ENV = set(re.findall(r'os\.(?:getenv|environ\.get)\("([A-Z_]+)"|_(?:bool|int|csv|doc_ids)\("([A-Z_]+)"',
                            inspect.getsource(cfg)))
CONFIG_ENV = {name for pair in CONFIG_ENV for name in pair if name} - set(GOOD_ENV)
assert {"WRITE_MODE", "ALLOW_DELETE_FEATURE", "ALLOW_FEATURESCRIPT", "ENABLE_LEGACY_SSE",
        "DISABLED_TOOLS", "PUBLIC_BASE_URL", "FLY_APP_NAME"} <= CONFIG_ENV


def _load(monkeypatch, **env):
    for key in list(cfg.os.environ):
        if key in CONFIG_ENV:
            monkeypatch.delenv(key, raising=False)
    for key, value in {**GOOD_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    return cfg.load_settings()


def _fly_env() -> dict[str, str]:
    import pathlib
    import tomllib

    fly = pathlib.Path(__file__).resolve().parent.parent / "fly.toml"
    return {k: str(v) for k, v in tomllib.loads(fly.read_text(encoding="utf-8"))["env"].items()}


def test_config_defaults(monkeypatch):
    s = _load(monkeypatch)
    assert s.write_allowed_name_prefix == "AI-"
    assert s.auth_tokens == {"x" * 40: "clickup"}


# AD-10: every security-relevant setting defaults to its most restrictive value.
def test_default_write_mode_is_off(monkeypatch):
    assert _load(monkeypatch).write_mode == "off"


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_write_mode_is_off(monkeypatch, value):
    assert _load(monkeypatch, WRITE_MODE=value).write_mode == "off"


@pytest.mark.parametrize("value", [None, "", "  "])
def test_default_delete_feature_is_off(monkeypatch, value):
    env = {} if value is None else {"ALLOW_DELETE_FEATURE": value}
    assert _load(monkeypatch, **env).allow_delete is False


@pytest.mark.parametrize("value", [None, "", "  "])
def test_default_featurescript_is_off(monkeypatch, value):
    env = {} if value is None else {"ALLOW_FEATURESCRIPT": value}
    assert _load(monkeypatch, **env).allow_featurescript is False


@pytest.mark.parametrize("value", [None, "", "  "])
def test_default_legacy_sse_is_off(monkeypatch, value):
    env = {} if value is None else {"ENABLE_LEGACY_SSE": value}
    assert _load(monkeypatch, **env).enable_legacy_sse is False


def test_default_disabled_tools_lock_exports(monkeypatch):
    assert _load(monkeypatch).disabled_tools == frozenset(cfg.DEFAULT_DISABLED_TOOLS)


def test_default_settings_expose_no_write_or_featurescript_tool(monkeypatch):
    s = _load(monkeypatch)
    enabled = {name for name in TOOL_CLASSES if is_tool_enabled(name, s)}
    assert not {name for name in enabled if TOOL_CLASSES[name] not in {READ, META, INDEX}}


@pytest.mark.parametrize("value,extra", [
    ("", set()),
    ("get_document", {"get_document"}),
    (" get_document , export_assembly ", {"get_document"}),
])
def test_disabled_tools_extend_default_lock(monkeypatch, value, extra):
    s = _load(monkeypatch, DISABLED_TOOLS=value)
    assert s.disabled_tools == frozenset(cfg.DEFAULT_DISABLED_TOOLS) | extra
    assert not is_tool_enabled("export_part_studio", s)


def test_fly_toml_keeps_production_behaviour(monkeypatch, tmp_path):
    env = _fly_env()
    s = _load(monkeypatch, **env)
    assert s.write_mode == "off"
    assert s.allow_featurescript is True and s.enable_legacy_sse is True
    assert s.allow_delete is False
    # Published tools equal those of the pre-H-1 production flags (delete was on, but
    # WRITE_MODE=off already hid the delete class).
    before = make_settings(tmp_path, write_mode="off", allow_delete=True, allow_featurescript=True,
                           enable_legacy_sse=True, write_allowed_document_ids=frozenset())
    now_tools = {n for n in TOOL_CLASSES if is_tool_enabled(n, s)}
    before_tools = {n for n in TOOL_CLASSES if is_tool_enabled(n, before)}
    assert now_tools == before_tools


@pytest.mark.parametrize("env", [
    {"MCP_AUTH_TOKENS": ""},
    {"MCP_AUTH_TOKENS": "short"},
    {"MCP_AUTH_TOKENS": "admin:" + "y" * 40},
    {"ADMIN_TOKEN": "short"},
    {"ADMIN_TOKEN": "x" * 40},
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


def test_admin_token_gets_reserved_label(monkeypatch):
    assert _load(monkeypatch, ADMIN_TOKEN="y" * 40).auth_tokens == {"x" * 40: "clickup", "y" * 40: "admin"}


def test_csv_export_neutralises_formulas():
    from onshape_secure.extra_tools import _csv_safe

    assert _csv_safe('=HYPERLINK("http://evil")') == "'=HYPERLINK(\"http://evil\")"
    assert [_csv_safe(v) for v in ("+1", "-1", "@SUM(A1)", "\tx")] == ["'+1", "'-1", "'@SUM(A1)", "'\tx"]
    assert _csv_safe("PRT-000593") == "PRT-000593" and _csv_safe(None) == ""


@pytest.mark.parametrize("path, expected", [
    ("/onshape/webhook/abcdefghijklmnop", "/onshape/webhook/abcdef..."),
    ("/img/abcdefghijklmnop.png", "/img/abcdef..."),
    ("/dl/abcdefghijklmnop/part.step", "/dl/abcdef..."),
    ("/img/", "/img/..."),
    ("/mcp", "/mcp"),
    ("/imgx/abcdefghijklmnop", "/imgx/abcdefghijklmnop"),
])
def test_redact_path_keeps_only_secret_prefix(path, expected):
    from onshape_secure.auth import redact_path

    assert redact_path(path) == expected


# --- H-9: read scope via tool class "index" (AD-14) -------------------------

# Published tools in production before H-9/H-10 (fly.toml: no read scope, no DISABLED_TOOLS).
PRODUCTION_TOOLS = {
    "capture_assembly_screenshot", "capture_part_screenshot", "capture_part_studio_screenshot",
    "check_assembly_interference", "eval_featurescript", "export_mapping_table", "export_released_drawing",
    "export_released_model", "find_drawing_for_part", "find_part_by_number", "find_part_studios", "get_assembly",
    "get_assembly_features", "get_assembly_positions", "get_body_details", "get_bounding_box", "get_document",
    "get_document_summary", "get_elements", "get_features", "get_parts", "get_variables", "list_documents",
    "search_documents", "search_part_index", "show_mapping_table",
}
# the hard-coded list is_tool_enabled used before the index class existed
OLD_READ_SCOPE_BLOCKLIST = {"list_documents", "search_documents", "show_mapping_table", "search_part_index",
                            "export_mapping_table"}


def test_fly_toml_publishes_same_tools_as_before_index_class(monkeypatch):
    s = _load(monkeypatch, **_fly_env())
    assert not s.read_restricted
    assert {n for n in TOOL_CLASSES if is_tool_enabled(n, s)} == PRODUCTION_TOOLS


def test_index_class_replaces_read_scope_name_list():
    assert {n for n, c in TOOL_CLASSES.items() if c == INDEX} == OLD_READ_SCOPE_BLOCKLIST


def test_read_scope_hides_index_class_not_names(monkeypatch, settings, tmp_path):
    monkeypatch.setitem(TOOL_CLASSES, "future_cross_document_tool", INDEX)
    scoped = make_settings(tmp_path, read_allowed_document_ids=frozenset({PROD_DOC}))
    assert is_tool_enabled("future_cross_document_tool", settings)
    assert not is_tool_enabled("future_cross_document_tool", scoped)


def test_read_scope_hides_exactly_the_index_class(settings, tmp_path):
    scoped = make_settings(tmp_path, read_allowed_document_ids=frozenset({PROD_DOC}))
    open_ = {n for n in TOOL_CLASSES if is_tool_enabled(n, settings)}
    hidden = open_ - {n for n in TOOL_CLASSES if is_tool_enabled(n, scoped)}
    assert hidden == {n for n in open_ if TOOL_CLASSES[n] == INDEX} == OLD_READ_SCOPE_BLOCKLIST


# --- H-10: tool registration lists agree (AD-2, AD-11) ----------------------

async def test_own_tool_registration_lists_agree():
    from onshape_mcp import server as upstream

    from onshape_secure.extra_tools import TOOLS, ExtraTools
    from onshape_secure.policy import EXTRA_TOOL_NAMES

    schemas = [t.name for t in TOOLS]
    assert len(schemas) == len(set(schemas)), "duplicate tool in extra_tools.TOOLS"
    handlers = set(ExtraTools(client=None, check_read=None).handlers())
    assert handlers == EXTRA_TOOL_NAMES, handlers ^ EXTRA_TOOL_NAMES
    assert set(schemas) <= EXTRA_TOOL_NAMES, set(schemas) - EXTRA_TOOL_NAMES
    assert EXTRA_TOOL_NAMES <= set(TOOL_CLASSES), EXTRA_TOOL_NAMES - set(TOOL_CLASSES)
    # own tools without an own schema replace an upstream tool (keeping its schema)
    upstream_names = {t.name for t in await upstream.list_tools()}
    assert EXTRA_TOOL_NAMES - set(schemas) <= upstream_names
    assert not set(schemas) & upstream_names, "own schema shadows an upstream tool"


def test_duplicate_own_tool_name_stops_startup(monkeypatch, settings):
    from onshape_secure import app as app_mod

    monkeypatch.setattr(app_mod, "EXTRA_TOOLS", [*app_mod.EXTRA_TOOLS, app_mod.EXTRA_TOOLS[0]])
    with pytest.raises(RuntimeError, match=f"duplicate tool names: {app_mod.EXTRA_TOOLS[0].name}"):
        app_mod.create_app(settings)


def test_own_tool_named_like_policy_tool_stops_startup(monkeypatch, settings):
    from mcp.types import Tool

    from onshape_secure import app as app_mod

    clash = Tool(name=app_mod.POLICY_TOOL, description="x", inputSchema={"type": "object", "properties": {}})
    monkeypatch.setattr(app_mod, "EXTRA_TOOLS", [*app_mod.EXTRA_TOOLS, clash])
    with pytest.raises(RuntimeError, match=f"duplicate tool names: {app_mod.POLICY_TOOL}"):
        app_mod.create_app(settings)


async def test_own_tool_colliding_with_upstream_stops_startup(monkeypatch, settings):
    from mcp.types import Tool

    from onshape_secure import app as app_mod

    clash = Tool(name="get_document", description="x", inputSchema={"type": "object", "properties": {}})
    monkeypatch.setattr(app_mod, "EXTRA_TOOLS", [*app_mod.EXTRA_TOOLS, clash])
    app = app_mod.create_app(settings)
    with pytest.raises(RuntimeError, match="duplicate tool names: get_document"):
        async with app.router.lifespan_context(app):
            pass


# --- DISABLED_TOOLS names are validated at startup ---------------------------

@pytest.mark.parametrize("names", [{"get_documnet"}, {"get_document", "nope"}])
def test_unknown_disabled_tool_stops_startup(tmp_path, names):
    from onshape_secure.app import create_app

    s = make_settings(tmp_path, disabled_tools=frozenset(cfg.DEFAULT_DISABLED_TOOLS) | names)
    with pytest.raises(cfg.ConfigError, match="unknown tool names: (get_documnet|nope)"):
        create_app(s)


def test_known_disabled_tools_start(tmp_path):
    from onshape_secure.app import create_app

    create_app(make_settings(tmp_path, disabled_tools=frozenset(cfg.DEFAULT_DISABLED_TOOLS) | {"get_document",
                                                                                                "show_mapping_table"}))


# --- H-5/H-6/H-7: one call scope for every Onshape request, fail-closed data directory ---


def test_every_public_upstream_client_method_is_metered(settings):
    from onshape_mcp import server as upstream

    from onshape_secure import app as app_mod

    app_mod.create_app(settings)
    cls = type(upstream.client)
    public = [n for n in dir(cls) if not n.startswith("_") and callable(getattr(cls, n))]
    for name in public:
        if name not in app_mod.CLIENT_NON_REQUEST_METHODS:
            assert getattr(getattr(upstream.client, name), "secure_metered", False), name
    assert {"get", "post", "delete"} <= set(public)


def test_new_upstream_client_method_stops_startup(monkeypatch, settings):
    from onshape_mcp.api.client import OnshapeClient

    from onshape_secure import app as app_mod

    async def get_raw(self, path):  # a new upstream helper that would bypass guard and budget
        return {}

    monkeypatch.setattr(OnshapeClient, "get_raw", get_raw, raising=False)
    with pytest.raises(RuntimeError, match="not covered by the call scope: get_raw"):
        app_mod.create_app(settings)


def test_new_upstream_http_verb_is_metered(monkeypatch, settings):
    from onshape_mcp import server as upstream
    from onshape_mcp.api.client import OnshapeClient

    from onshape_secure import app as app_mod

    async def put(self, path, data=None):
        return {}

    monkeypatch.setattr(OnshapeClient, "put", put, raising=False)
    app_mod.create_app(settings)
    try:
        assert upstream.client.put.secure_metered
    finally:
        del upstream.client.put  # the wrapper lives on the shared singleton


async def test_onshape_request_outside_call_scope_is_refused(settings, monkeypatch):
    import httpx
    from onshape_mcp import server as upstream

    from onshape_secure import app as app_mod

    sent = []
    app_mod.create_app(settings)
    monkeypatch.setattr(upstream.client, "_client", httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200, json={}))))
    with pytest.raises(PolicyDenied, match="outside a call scope"):
        await upstream.client.get("/api/v6/documents/" + "a" * 24)
    assert sent == []
    assert not os.path.exists(settings.usage_file)  # no budget unit reserved


@pytest.mark.parametrize("make_path", [
    lambda tmp: tmp / "missing" / "usage.json",  # directory does not exist
    lambda tmp: tmp / "file.txt" / "usage.json",  # "directory" is a file
])
def test_unusable_data_dir_stops_startup(tmp_path, make_path):
    from onshape_secure import app as app_mod

    (tmp_path / "file.txt").write_text("x")
    settings = make_settings(tmp_path, usage_file=str(make_path(tmp_path)))
    with pytest.raises(app_mod.DataDirError, match="not writable"):
        app_mod.create_app(settings)


def test_data_dir_error_is_a_config_error():
    from onshape_secure import app as app_mod

    assert issubclass(app_mod.DataDirError, cfg.ConfigError)


def test_broken_mapping_db_stops_startup(tmp_path):
    from onshape_secure import app as app_mod

    (tmp_path / "mappings.db").write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(app_mod.DataDirError, match="mapping table"):
        app_mod.create_app(make_settings(tmp_path))


def test_unopenable_image_store_stops_startup(tmp_path):
    from onshape_secure import app as app_mod

    (tmp_path / "blocker").write_text("x")
    settings = make_settings(tmp_path, image_links=True, public_base_url="https://x.example",
                             image_dir=str(tmp_path / "blocker" / "images"))
    with pytest.raises(app_mod.DataDirError, match="image store"):
        app_mod.create_app(settings)


def test_main_exits_2_when_data_dir_unusable(monkeypatch, tmp_path, capsys):
    from onshape_secure import app as app_mod

    settings = make_settings(tmp_path, usage_file=str(tmp_path / "missing" / "usage.json"))
    monkeypatch.setattr(app_mod, "load_settings", lambda: settings)
    with pytest.raises(SystemExit) as exc:
        app_mod.main()
    assert exc.value.code == 2
    assert "not writable" in capsys.readouterr().err


def test_writable_data_dir_starts_and_leaves_no_probe(tmp_path):
    from onshape_secure import app as app_mod

    app_mod.create_app(make_settings(tmp_path))
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".write-probe")]
    assert (tmp_path / "mappings.db").exists()


def test_audit_lines_only_come_from_audit():
    import pathlib

    from onshape_secure import app as app_mod

    src = pathlib.Path(app_mod.__file__).parent
    hits = [f"{p.name}:{i}" for p in src.glob("*.py") for i, line in enumerate(p.read_text("utf-8").splitlines(), 1)
            if "AUDIT" in line]
    assert len(hits) == 1, hits  # the logger call inside _audit
    assert 'logger.info("AUDIT " + json.dumps(' in inspect.getsource(app_mod._audit)


def test_nothing_reaches_onshape_around_the_client_methods():
    """AD-3: only the upstream client itself and get_bytes (via _send) touch the raw httpx client or sign requests."""
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "src"
    raw = re.compile(r"\._client\b|_get_auth_header|httpx\.(Async)?Client\(")
    hits = [(p.relative_to(root).as_posix(), line.strip()) for p in root.rglob("*.py")
            for line in p.read_text("utf-8").splitlines() if raw.search(line)]
    outside = [h for h in hits if h[0] not in ("onshape_mcp/api/client.py", "onshape_secure/app.py")]
    assert outside == []
    app_src = (root / "onshape_secure/app.py").read_text("utf-8")
    get_bytes = app_src[app_src.index("    async def get_bytes("):app_src.index('return await _send("get", path, download)')]
    assert [line for f, line in hits if f == "onshape_secure/app.py" and line not in get_bytes] == []
