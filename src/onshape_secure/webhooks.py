"""Receiver for Onshape release webhooks: keeps the mapping table fresh without polling.

Onshape posts to https://<host>/onshape/webhook/<secret>. Notifications don't count against
the API limit. The random secret in the path is the only credential (Onshape can't send our
bearer token), compared in constant time. Payloads only *invalidate* cache rows and record
`webhook_last_event_at`; they never write part data or raise trust (AD-7): `webhook_verified_at`
is set only by the registration and the metered health GET, so a forged ping cannot extend
cache lifetimes. Worst case per forged call: every document is re-indexed on its next lookup
(a few Onshape calls each, within the daily budget cap).
"""

from __future__ import annotations

import hmac
import json
import re
import time

from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .mapping import MappingStore

MAX_BODY = 64 * 1024
RELEASE_EVENTS = ["onshape.revision.created", "onshape.workflow.transition"]
METADATA_EVENT = "onshape.model.lifecycle.metadata"
_HEX_ID = re.compile(r"[0-9a-f]{24}")


def _hex_id(value: object) -> str | None:
    return value if isinstance(value, str) and _HEX_ID.fullmatch(value) else None


def _document_id(event: dict) -> str | None:
    return _hex_id(event.get("documentId"))


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
        # once registered, only events of our own webhooks count (missing id = unknown)
        wid = event.get("webhookId")
        if known and not (isinstance(wid, str) and wid in known):
            logger.warning(f"webhook event for unknown webhook ignored: {json.dumps(name)}")
            return JSONResponse({"ok": True})  # 200: Onshape must not retry or disable the hook
        mapping.set_meta("webhook_last_event_at", str(time.time()))
        did = _document_id(event)
        scope = "-"
        # webhook.register / webhook.ping: nothing to do - payloads never raise trust (AD-7)
        if name == "onshape.revision.created":
            part_number = str(event.get("partNumber") or "")[:64] or None
            etype = event.get("elementType")
            mapping.on_revision_created(part_number, etype if isinstance(etype, int) else None)
        elif name == "onshape.workflow.transition":
            otype = event.get("objectType")
            scope, affected = mapping.on_workflow_transition(
                _hex_id(event.get("objectId")), otype if isinstance(otype, str) else None
            )
            for doc in affected:
                mapping.mark_doc_dirty(doc)
        if name in RELEASE_EVENTS and did:
            # state/revision in the working-state index changed; never mark all (budget)
            mapping.mark_doc_dirty(did)
        elif name == METADATA_EVENT:
            # part number/name may have changed; no documentId -> all documents, a malformed one -> none
            if did or not event.get("documentId"):
                mapping.mark_doc_dirty(did)
        logger.info(
            "webhook event={} part={} doc={} object={} transition={} invalidated={}",
            json.dumps(name),
            json.dumps(str(event.get("partNumber") or "-")[:64]),
            "yes" if did else "no",  # shows whether release events carry documentId
            json.dumps(str(event.get("objectType") or "-")[:32]),  # workflow events: real values are undocumented
            json.dumps(str(event.get("transitionName") or "-")[:64]),
            scope,  # workflow events: parts:N | none | all; other events: -
        )
        return JSONResponse({"ok": True})

    return receive
