"""Configuration loaded from environment variables (fail closed on anything invalid)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

ONSHAPE_ID_RE = re.compile(r"^[0-9a-f]{24}$")
TOKEN_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
MIN_TOKEN_LENGTH = 32
ADMIN_LABEL = "admin"  # only this client may call /admin/* (token from ADMIN_TOKEN)


class ConfigError(ValueError):
    """Raised when the environment does not describe a safe configuration."""


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true/false, got {raw!r}")


def _int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _doc_ids(name: str) -> frozenset[str]:
    ids = []
    for item in _csv(name):
        item = item.lower()
        if not ONSHAPE_ID_RE.match(item):
            raise ConfigError(f"{name}: {item!r} is not a 24-character Onshape document ID")
        ids.append(item)
    return frozenset(ids)


def _tokens() -> dict[str, str]:
    """Parse MCP_AUTH_TOKENS = "label:token,label2:token2" (label optional)."""
    entries = _csv("MCP_AUTH_TOKENS")
    if not entries:
        raise ConfigError("MCP_AUTH_TOKENS is required (generate with: openssl rand -hex 32)")
    tokens: dict[str, str] = {}
    for idx, entry in enumerate(entries):
        if ":" in entry:
            label, token = entry.split(":", 1)
        else:
            label, token = f"client{idx + 1}", entry
        label, token = label.strip(), token.strip()
        if not TOKEN_LABEL_RE.match(label):
            raise ConfigError(f"MCP_AUTH_TOKENS: invalid label {label!r}")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"MCP_AUTH_TOKENS: token for {label!r} is shorter than {MIN_TOKEN_LENGTH} characters"
            )
        if label == ADMIN_LABEL:
            raise ConfigError(f"MCP_AUTH_TOKENS: label {ADMIN_LABEL!r} is reserved (use ADMIN_TOKEN)")
        if token in tokens:
            raise ConfigError("MCP_AUTH_TOKENS: duplicate token")
        tokens[token] = label
    admin = os.getenv("ADMIN_TOKEN", "").strip()
    if admin:
        if len(admin) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"ADMIN_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters")
        if admin in tokens:
            raise ConfigError("ADMIN_TOKEN must differ from all MCP_AUTH_TOKENS")
        tokens[admin] = ADMIN_LABEL
    return tokens


def _base_url() -> str:
    raw = os.getenv("ONSHAPE_BASE_URL", "https://cad.onshape.com").strip().rstrip("/")
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "onshape.com" or host.endswith(".onshape.com")):
        raise ConfigError("ONSHAPE_BASE_URL must be an https://*.onshape.com URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
        raise ConfigError("ONSHAPE_BASE_URL must not contain a path, query or credentials")
    return f"https://{host}" + (f":{parsed.port}" if parsed.port else "")


def _public_base_url() -> str:
    raw = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not raw and os.getenv("FLY_APP_NAME"):
        raw = f"https://{os.environ['FLY_APP_NAME']}.fly.dev"
    if not raw:
        return ""
    parsed = urlparse(raw)
    local = parsed.hostname in {"localhost", "127.0.0.1"}
    if parsed.scheme != "https" and not (local and parsed.scheme == "http"):
        raise ConfigError("PUBLIC_BASE_URL must be an https:// URL")
    if parsed.path not in {"", "/"} or parsed.query or parsed.username:
        raise ConfigError("PUBLIC_BASE_URL must not contain a path, query or credentials")
    return raw


# Tools that are hidden unless explicitly re-enabled. The export tools only start a
# translation job inside Onshape; the result cannot be downloaded through ClickUp.
DEFAULT_DISABLED_TOOLS = ("export_part_studio", "export_assembly")


@dataclass(frozen=True)
class Settings:
    onshape_access_key: str
    onshape_secret_key: str
    onshape_base_url: str
    auth_tokens: dict[str, str]  # token -> label
    write_mode: str  # off | allowlist | all
    write_allowed_document_ids: frozenset[str]
    write_allowed_name_prefix: str
    read_allowed_document_ids: frozenset[str]
    allow_delete: bool
    allow_featurescript: bool
    disabled_tools: frozenset[str]
    tool_calls_per_minute: int
    max_concurrent_calls: int
    onshape_daily_limit: int
    onshape_monthly_limit: int
    onshape_weekly_limit: int
    onshape_yearly_limit: int
    usage_file: str
    max_response_chars: int
    max_request_bytes: int
    enable_legacy_sse: bool
    allowed_hosts: tuple[str, ...]
    log_level: str
    host: str
    port: int
    image_links: bool = False
    image_ttl_hours: int = 24
    public_base_url: str = ""
    image_dir: str = ""
    cache_max_mb: int = 500
    file_cache_hours: int = 24

    @property
    def read_restricted(self) -> bool:
        return bool(self.read_allowed_document_ids)


def load_settings() -> Settings:
    access_key = os.getenv("ONSHAPE_ACCESS_KEY", "").strip()
    secret_key = os.getenv("ONSHAPE_SECRET_KEY", "").strip()
    if not access_key or not secret_key:
        raise ConfigError("ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY are required")

    write_mode = os.getenv("WRITE_MODE", "allowlist").strip().lower()
    if write_mode not in {"off", "allowlist", "all"}:
        raise ConfigError("WRITE_MODE must be one of: off, allowlist, all")

    prefix = os.getenv("WRITE_ALLOWED_NAME_PREFIX", "AI-")
    if len(prefix) > 40 or any(ord(c) < 32 for c in prefix):
        raise ConfigError("WRITE_ALLOWED_NAME_PREFIX is too long or contains control characters")
    if prefix and len(prefix.strip()) < 2:
        raise ConfigError("WRITE_ALLOWED_NAME_PREFIX must be at least 2 visible characters (or empty)")

    disabled_raw = os.getenv("DISABLED_TOOLS")
    disabled = (
        frozenset(DEFAULT_DISABLED_TOOLS) if disabled_raw is None else frozenset(_csv("DISABLED_TOOLS"))
    )

    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")

    return Settings(
        onshape_access_key=access_key,
        onshape_secret_key=secret_key,
        onshape_base_url=_base_url(),
        auth_tokens=_tokens(),
        write_mode=write_mode,
        write_allowed_document_ids=_doc_ids("WRITE_ALLOWED_DOCUMENT_IDS"),
        write_allowed_name_prefix=prefix,
        read_allowed_document_ids=_doc_ids("READ_ALLOWED_DOCUMENT_IDS"),
        allow_delete=_bool("ALLOW_DELETE_FEATURE", True),
        allow_featurescript=_bool("ALLOW_FEATURESCRIPT", True),
        disabled_tools=disabled,
        tool_calls_per_minute=_int("TOOL_CALLS_PER_MINUTE", 30, 1, 600),
        max_concurrent_calls=_int("MAX_CONCURRENT_CALLS", 4, 1, 32),
        onshape_daily_limit=_int("ONSHAPE_DAILY_REQUEST_LIMIT", 500, 0),
        onshape_weekly_limit=_int("ONSHAPE_WEEKLY_REQUEST_LIMIT", 600, 0),
        onshape_monthly_limit=_int("ONSHAPE_MONTHLY_REQUEST_LIMIT", 1000, 0),
        onshape_yearly_limit=_int("ONSHAPE_YEARLY_REQUEST_LIMIT", 2400, 0),
        usage_file=os.getenv("USAGE_FILE", "/data/usage.json").strip(),
        max_response_chars=_int("MAX_RESPONSE_CHARS", 60_000, 1_000, 2_000_000),
        max_request_bytes=_int("MAX_REQUEST_BYTES", 1_000_000, 10_000, 20_000_000),
        enable_legacy_sse=_bool("ENABLE_LEGACY_SSE", True),
        allowed_hosts=tuple(h.lower() for h in _csv("ALLOWED_HOSTS")),
        log_level=log_level,
        host=os.getenv("HOST", "0.0.0.0").strip(),
        port=_int("PORT", 8080, 1, 65535),
        image_links=_bool("IMAGE_LINKS", True),
        image_ttl_hours=_int("IMAGE_LINK_TTL_HOURS", 24, 1, 168),
        public_base_url=_public_base_url(),
        image_dir=os.getenv("IMAGE_DIR", "").strip(),
        cache_max_mb=_int("CACHE_MAX_MB", 500, 10, 50_000),
        file_cache_hours=_int("FILE_CACHE_HOURS", 24, 1, 720),
    )
