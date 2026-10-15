"""Persistent caches that save Onshape API calls, plus short-lived download links.

Released versions in Onshape are immutable, so anything derived from a version
(exports, renders) can be reused forever. Everything lives on the Fly volume so the
cache survives the frequent auto-stop of the machine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from collections import defaultdict
from typing import Any

from loguru import logger
from starlette.requests import Request
from starlette.responses import Response


class KVCache:
    """Small JSON key/value store with per-entry expiry (company id, revisions, pending jobs)."""

    MAX_ENTRIES = 5000

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self._data = {k: v for k, v in loaded.items() if isinstance(v, dict) and "v" in v}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning(f"cache file unreadable, starting empty: {exc}")

    def get(self, key: str) -> Any:
        entry = self._data.get(key)
        if not entry:
            return None
        exp = entry.get("exp")
        if exp is not None and exp < time.time():
            self._data.pop(key, None)
            return None
        return entry["v"]

    def set(self, key: str, value: Any, ttl_seconds: float | None = None) -> None:
        self._data[key] = {"v": value, "exp": (time.time() + ttl_seconds) if ttl_seconds else None}
        self._save()

    def delete(self, key: str) -> None:
        if self._data.pop(key, None) is not None:
            self._save()

    def _save(self) -> None:
        now = time.time()
        self._data = {k: v for k, v in self._data.items() if v.get("exp") is None or v["exp"] >= now}
        if len(self._data) > self.MAX_ENTRIES:  # drop oldest-expiring first
            keep = sorted(self._data.items(), key=lambda kv: kv[1].get("exp") or float("inf"))
            self._data = dict(keep[-self.MAX_ENTRIES :])
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning(f"could not write cache file: {exc}")


class FileCache:
    """Short-lived cache for big results (STEP, PDF, renders): deleted ttl seconds after creation."""

    def __init__(self, directory: str, max_bytes: int, ttl_seconds: int = 24 * 3600):
        self.directory = directory
        self.max_bytes = max_bytes
        self.ttl = ttl_seconds
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    def key_hash(key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:40]

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks[self.key_hash(key)]

    def path_for(self, key: str) -> str:
        return os.path.join(self.directory, self.key_hash(key))

    def get(self, key: str) -> str | None:
        path = self.path_for(key)
        try:
            if time.time() - os.path.getmtime(path) > self.ttl:
                os.remove(path)
                return None
            return path
        except OSError:
            return None

    def put(self, key: str, data: bytes) -> str:
        path = self.path_for(key)
        tmp = f"{path}.{secrets.token_hex(4)}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        self._evict()
        return path

    def _evict(self) -> None:
        try:
            entries = [e for e in os.scandir(self.directory) if e.is_file() and not e.name.endswith(".tmp")]
        except OSError:
            return
        now = time.time()
        total = sum(e.stat().st_size for e in entries)
        for entry in sorted(entries, key=lambda e: e.stat().st_mtime):
            if total <= self.max_bytes and now - entry.stat().st_mtime <= self.ttl:
                continue
            try:
                size = entry.stat().st_size
                os.remove(entry.path)
                total -= size
            except OSError:
                pass


SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")


class DownloadLinks:
    """Unguessable, expiring download URLs: https://<host>/dl/<token>/<filename>."""

    def __init__(self, directory: str, ttl_seconds: int, base_url: str):
        self.directory = directory
        self.ttl = ttl_seconds
        self.base_url = base_url.rstrip("/")
        os.makedirs(directory, mode=0o700, exist_ok=True)

    def _cleanup(self) -> None:
        now = time.time()
        try:
            for entry in os.scandir(self.directory):
                if entry.is_file() and now - entry.stat().st_mtime > self.ttl:
                    os.remove(entry.path)
        except OSError:
            pass

    def create(self, source_path: str, filename: str, media_type: str) -> str:
        self._cleanup()
        token = secrets.token_urlsafe(24)
        filename = SAFE_FILENAME_RE.sub("_", filename).strip("._")[:120] or "download"
        target = os.path.join(self.directory, token)
        try:
            os.link(source_path, target)  # same volume: no copy
        except OSError:
            with open(source_path, "rb") as src, open(target, "wb") as dst:
                dst.write(src.read())
        with open(f"{target}.json", "w", encoding="utf-8") as fh:
            json.dump({"filename": filename, "media_type": media_type}, fh)
        return f"{self.base_url}/dl/{token}/{filename}"

    async def serve(self, request: Request) -> Response:
        token = request.path_params.get("token", "")
        if not TOKEN_RE.match(token):
            return Response(status_code=404)
        path = os.path.join(self.directory, token)
        try:
            age = time.time() - os.path.getmtime(path)
            if age > self.ttl:
                for p in (path, f"{path}.json"):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                return Response(status_code=404)
            with open(f"{path}.json", encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(path, "rb") as fh:
                body = fh.read()
        except (OSError, ValueError):
            return Response(status_code=404)
        logger.info(f"download_served token={token[:6]}… file={meta['filename']!r}")
        inline = meta["media_type"] in {"application/pdf", "image/png"}  # CSV/STEP: download
        disposition = "inline" if inline else "attachment"
        headers = {
            "Content-Disposition": f'{disposition}; filename="{meta["filename"]}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex",
            "Content-Length": str(len(body)),
        }
        if meta["media_type"] != "application/pdf":  # a CSP sandbox would block the browser PDF viewer
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return Response(body if request.method == "GET" else b"", media_type=meta["media_type"], headers=headers)
