import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# upstream reads these at import time
os.environ.setdefault("ONSHAPE_ACCESS_KEY", "test-access")
os.environ.setdefault("ONSHAPE_SECRET_KEY", "test-secret")

from onshape_secure.config import Settings  # noqa: E402

TOKEN = "t" * 40
OTHER_TOKEN = "o" * 40
ADMIN_TOKEN = "z" * 40
AI_DOC = "a" * 24  # name starts with prefix -> writable
PROD_DOC = "b" * 24  # production document -> read only
LISTED_DOC = "c" * 24  # allow-listed by id
WS = "d" * 24
EL = "e" * 24


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        onshape_access_key="test-access",
        onshape_secret_key="test-secret",
        onshape_base_url="https://cad.onshape.com",
        auth_tokens={TOKEN: "clickup", OTHER_TOKEN: "eric", ADMIN_TOKEN: "admin"},
        write_mode="allowlist",
        write_allowed_document_ids=frozenset({LISTED_DOC}),
        write_allowed_name_prefix="AI-",
        read_allowed_document_ids=frozenset(),
        allow_delete=True,
        allow_featurescript=True,
        disabled_tools=frozenset({"export_part_studio", "export_assembly"}),
        tool_calls_per_minute=100,
        max_concurrent_calls=4,
        onshape_daily_limit=1000,
        onshape_monthly_limit=10000,
        onshape_yearly_limit=100000,
        onshape_weekly_limit=100000,
        usage_file=str(tmp_path / "usage.json"),
        max_response_chars=60_000,
        max_request_bytes=1_000_000,
        enable_legacy_sse=True,
        allowed_hosts=(),
        log_level="WARNING",
        host="127.0.0.1",
        port=0,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)
