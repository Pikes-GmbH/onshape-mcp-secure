"""Bearer-token authentication as plain ASGI middleware (streaming-safe)."""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import time

from loguru import logger
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

# label of the authenticated client for the current request (used in audit logs/rate limits)
current_client: contextvars.ContextVar[str] = contextvars.ContextVar("current_client", default="-")

PUBLIC_PATHS = {"/healthz"}
AUTH_FAIL_LOGS_PER_MINUTE = 20


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:8]


class BearerAuthMiddleware:
    """Accepts `Authorization: Bearer <token>` or `X-API-Key: <token>`."""

    def __init__(self, app, tokens: dict[str, str]):
        self.app = app
        # compare against every token in constant time; keep (token bytes, label, fingerprint)
        self._tokens = [(t.encode(), label, token_fingerprint(t)) for t, label in tokens.items()]
        self._fail_window_start = 0.0
        self._fail_logged = 0
        self._fail_suppressed = 0

    def _match(self, presented: str) -> tuple[str, str] | None:
        candidate = presented.encode()
        found = None
        for token, label, fingerprint in self._tokens:
            if hmac.compare_digest(candidate, token):
                found = (label, fingerprint)
        return found

    @staticmethod
    def _extract(headers: dict[bytes, bytes]) -> str:
        auth = headers.get(b"authorization", b"").decode("latin-1").strip()
        if auth[:7].lower() == "bearer ":
            return auth[7:].strip()
        return headers.get(b"x-api-key", b"").decode("latin-1").strip()

    def _log_failure(self, scope, headers) -> None:
        now = time.monotonic()
        if now - self._fail_window_start > 60:
            if self._fail_suppressed:
                logger.warning(f"auth_failed: {self._fail_suppressed} further failures not logged")
            self._fail_window_start, self._fail_logged, self._fail_suppressed = now, 0, 0
        if self._fail_logged >= AUTH_FAIL_LOGS_PER_MINUTE:
            self._fail_suppressed += 1
            return
        self._fail_logged += 1
        client_ip = headers.get(b"fly-client-ip", b"").decode("latin-1") or (
            (scope.get("client") or ("?",))[0]
        )
        # json.dumps escapes newlines etc. -> no forged log lines
        logger.warning(
            "auth_failed path={} ip={}",
            json.dumps(str(scope.get("path", ""))[:200]),
            json.dumps(client_ip[:64]),
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":  # websockets etc. are never served
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        path = scope.get("path", "")
        # /img/<random>.png and /dl/<random>/<file> are capability URLs (unguessable, short-lived) so chat UIs can show them
        public_prefix = path.startswith("/img/") or path.startswith("/dl/")
        webhook = path.startswith("/onshape/webhook/") and scope.get("method") == "POST"  # secret path
        if path in PUBLIC_PATHS or webhook or (public_prefix and scope.get("method") in ("GET", "HEAD")):
            return await self.app(scope, receive, send)

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        presented = self._extract(headers)
        match = self._match(presented) if presented else None
        if match is None:
            self._log_failure(scope, headers)
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b'Bearer realm="onshape-mcp"'),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        label, fingerprint = match
        # lets the MCP SDK bind legacy SSE sessions to the client that opened them
        scope = dict(scope)
        scope["user"] = AuthenticatedUser(
            AccessToken(token=fingerprint, client_id=label, scopes=[], subject=label)
        )
        token_ctx = current_client.set(label)
        try:
            await self.app(scope, receive, send)
        finally:
            current_client.reset(token_ctx)
