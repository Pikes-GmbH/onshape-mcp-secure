"""Receiver for Onshape release webhooks: keeps the mapping table fresh without polling.

Onshape posts to https://<host>/onshape/webhook/<secret>. Notifications don't count against
the API limit. The random secret in the path is the only credential (Onshape can't send our
bearer token), compared in constant time; payloads are only used to *invalidate* cache rows,
never to write data, so a forged call can at worst cause a few extra lookups.
"""

from __future__ import annotations

import hmac
import json
import time

from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .mapping import MappingStore

MAX_BODY = 64 * 1024
RELEASE_EVENTS = ["onshape.revision.created", "onshape.workflow.transition"]
METADATA_EVENT = "onshape.model.lifecycle.metadata"


def make_receiver(mapping: MappingStore):
    async def receive(request: Request) -> Response:
        secret = request.path_params.get("secret", "")
        if not hmac.compare_digest(secret.encode(), mapping.webhook_secret().encode()):
            return Response(status_code=404)
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response(status_code=413)
        try:
            event = json.loads(body or b"{}")
        except ValueError:
            return Response(status_code=400)
        if not isinstance(event, dict):
            return Response(status_code=400)
        name = str(event.get("event", ""))[:80]
        known = {w for w in (mapping.meta("webhook_id"), mapping.meta("webhook_meta_id")) if w}
        if known and event.get("webhookId") and event.get("webhookId") not in known:
            logger.warning(f"webhook event for unknown webhook ignored: {name}")
            return JSONResponse({"ok": True})
        now = str(time.time())
        mapping.set_meta("webhook_last_event_at", now)
        if name in ("webhook.register", "webhook.ping"):
            mapping.set_meta("webhook_verified_at", now)
        elif name == "onshape.revision.created":
            part_number = str(event.get("partNumber") or "")[:64] or None
            etype = event.get("elementType")
            mapping.on_revision_created(part_number, etype if isinstance(etype, int) else None)
        elif name == "onshape.workflow.transition":
            mapping.invalidate_all()  # e.g. obsoletion - rare, re-lookups are cheap
        elif name == "onshape.model.lifecycle.metadata":
            did = str(event.get("documentId") or "")
            mapping.mark_doc_dirty(did if len(did) == 24 else None)  # part number/name may have changed
        logger.info(f"webhook event={name} part={str(event.get('partNumber') or '-')[:64]}")
        return JSONResponse({"ok": True})

    return receive
