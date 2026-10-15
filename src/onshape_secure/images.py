"""Short-lived, unguessable image links so chat UIs like ClickUp can display screenshots.

MCP clients don't always render image content returned by a tool. Each screenshot is
therefore also stored on the volume and exposed at https://<host>/img/<random>.png
for a limited time. The 192-bit random name is the only "key" (capability URL).
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import time

from loguru import logger
from starlette.requests import Request
from starlette.responses import Response

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{32}\.png$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FILES = 500


class ImageStore:
    def __init__(self, directory: str, ttl_seconds: int, base_url: str):
        self.directory = directory
        self.ttl = ttl_seconds
        self.base_url = base_url.rstrip("/")
        os.makedirs(directory, mode=0o700, exist_ok=True)

    def _cleanup(self) -> None:
        now = time.time()
        try:
            entries = sorted(
                (e for e in os.scandir(self.directory) if e.is_file()), key=lambda e: e.stat().st_mtime
            )
        except OSError:
            return
        for idx, entry in enumerate(entries):
            expired = now - entry.stat().st_mtime > self.ttl
            too_many = len(entries) - idx > MAX_FILES
            if expired or too_many:
                try:
                    os.remove(entry.path)
                except OSError:
                    pass

    def save_base64(self, data_b64: str) -> str | None:
        try:
            raw = base64.b64decode(data_b64, validate=True)
        except (ValueError, TypeError):
            return None
        if not raw.startswith(PNG_MAGIC) or len(raw) > MAX_IMAGE_BYTES:
            return None
        self._cleanup()
        name = f"{secrets.token_urlsafe(24)}.png"  # 32 chars, 192 bit
        path = os.path.join(self.directory, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        return f"{self.base_url}/img/{name}"

    async def serve(self, request: Request) -> Response:
        name = request.path_params.get("name", "")
        if not NAME_RE.match(name):
            return Response(status_code=404)
        path = os.path.join(self.directory, name)
        try:
            age = time.time() - os.path.getmtime(path)
            if age > self.ttl:
                os.remove(path)
                return Response(status_code=404)
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return Response(status_code=404)
        ua = request.headers.get("user-agent", "")[:80]
        logger.info(f"image_served name={name[:6]}… ua={ua!r}")
        return Response(
            body if request.method == "GET" else b"",
            media_type="image/png",
            headers={
                "Cache-Control": f"private, max-age={max(0, int(self.ttl - age))}",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": "inline",
                "Referrer-Policy": "no-referrer",
                "X-Robots-Tag": "noindex",
                "Content-Security-Policy": "default-src 'none'",
                "Content-Length": str(len(body)),
            },
        )
