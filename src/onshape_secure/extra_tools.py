"""Tools added by this server, built for minimal Onshape API usage.

* find_part_by_number      - release-management lookup (1 call, cached)
* capture_part_screenshot  - one part / assembly; renders of released versions are cached forever
* export_released_model    - STEP of a released part or assembly (POST + 1-2 polls + download, cached forever)
* export_released_drawing  - PDF/DXF/DWG of the drawing (same flow, cached forever)

Released versions are immutable, so every derived file is reused across calls and restarts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from mcp.types import ImageContent, TextContent, Tool

from loguru import logger

from .cache import DownloadLinks, FileCache, KVCache
from .mapping import MappingStore
from .policy import AUTO_VIEW_MATRIX, PATH_SAFE_ID_RE, PolicyDenied

PART_NUMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.#+\-]{0,63}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9.\-]{1,16}$")
HEX24 = re.compile(r"^[0-9a-f]{24}$")
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
ISO_VIEW = "0.612,0.612,0,0,-0.354,0.354,0.707,0,0.707,-0.707,0.707,0"
NAMED = {"top", "bottom", "front", "back", "left", "right"}
PARTSTUDIO, ASSEMBLY, DRAWING = 0, 1, 2
ELEMENT_NAMES = {PARTSTUDIO: "PARTSTUDIO", ASSEMBLY: "ASSEMBLY", DRAWING: "DRAWING"}

# poll schedule (seconds) - most part exports finish before the first poll
POLL_DELAYS_NEW = (5, 4, 6, 8, 10, 12, 13)
POLL_DELAYS_RESUME = (2, 4, 6, 8, 10, 12)
REVISION_TTL_LATEST = 3600  # "latest revision" may change when re-released
REVISION_TTL_FIXED = 30 * 86400
NOT_FOUND_TTL = 3600
PENDING_TTL = 1800
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_INDEX_DOCS_PER_LOOKUP = 3  # documents (re)indexed at most per lookup (1 call each)

DRAWING_FORMATS = {
    "PDF": ("pdf", "application/pdf"),
    "DXF": ("dxf", "image/vnd.dxf"),
    "DWG": ("dwg", "image/vnd.dwg"),
}

_PN = {"type": "string", "description": "Onshape part number, e.g. PRT-000593."}
_REV = {"type": "string", "description": "Optional revision, e.g. A. Default: latest released."}

TOOLS = [
    Tool(
        name="find_part_by_number",
        description=(
            "Find a part/assembly/drawing by part number (e.g. PRT-000583). Released items come from release "
            "management, never-released parts from the server's part-number index of the working state. "
            "Returns onshapeUrl and whether it is released. ALWAYS use this for part numbers - never search, list "
            "or browse documents to find a part number (that burns the small Onshape API budget). If it says not "
            "found, ask the user for the document."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "partNumber": _PN,
                "revision": _REV,
                "documentId": {"type": "string", "description": "Only if a previous call said not found: the document the user named."},
            },
            "required": ["partNumber"],
        },
    ),
    Tool(
        name="find_drawing_for_part",
        description=(
            "Which drawing belongs to a released part? Returns drawing number (e.g. DRW-000635), revision and "
            "release state via Onshape Where Used. Answered from the server's mapping table when known."
        ),
        inputSchema={"type": "object", "properties": {"partNumber": _PN, "revision": _REV}, "required": ["partNumber"]},
    ),
    Tool(
        name="export_mapping_table",
        description=(
            "Download the COMPLETE mapping table (all releases, drawing links, unreleased indexed parts) as a "
            "CSV/Excel file link. Costs no Onshape API calls."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="search_part_index",
        description=(
            "Search the server's part index (working state of indexed documents) by part name or partial part "
            "number, e.g. 'Mitnehmer'. Costs NO Onshape API calls - use it instead of browsing Onshape."
        ),
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Part name or part-number fragment."}},
            "required": ["text"],
        },
    ),
    Tool(
        name="show_mapping_table",
        description=(
            "Show the server's stored mapping table (part number -> revision -> drawing). Costs no Onshape API "
            "calls. Optional partNumber filter with * wildcard, e.g. PRT-0005*."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "partNumber": {"type": "string", "description": "Optional filter, * as wildcard."},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="capture_part_screenshot",
        description=(
            "Render ONE released part (or released assembly) by part number, or one part by IDs "
            "(documentId+wvm+wvmId+elementId+partId). Include the returned markdown image line unchanged in "
            "your answer."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "partNumber": _PN,
                "revision": _REV,
                "documentId": {"type": "string"},
                "wvm": {"type": "string", "enum": ["w", "v", "m"]},
                "wvmId": {"type": "string"},
                "elementId": {"type": "string"},
                "partId": {"type": "string"},
                "view": {"type": "string", "description": "auto (default), iso, top, bottom, front, back, left, right", "default": "auto"},
                "outputWidth": {"type": "integer", "default": 1000},
                "outputHeight": {"type": "integer", "default": 750},
            },
        },
    ),
    Tool(
        name="export_released_model",
        description=(
            "Export the 3D model of a RELEASED part or assembly as STEP and return a download link (valid 24 h). "
            "Files are cached, repeated requests cost no Onshape API calls. If the result says the export is "
            "still running, call the tool again with the same arguments (it resumes, it does not restart)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"partNumber": _PN, "revision": _REV},
            "required": ["partNumber"],
        },
    ),
    Tool(
        name="export_released_drawing",
        description=(
            "Export the drawing of a RELEASED part as PDF (default), DXF or DWG and return a download link "
            "(valid 24 h). Finds the drawing by the part number (or drawingNumber). Cached like "
            "export_released_model; call again with the same arguments if the export is still running."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "partNumber": _PN,
                "revision": _REV,
                "drawingNumber": {"type": "string", "description": "Optional: part number of the drawing itself."},
                "drawingElementId": {"type": "string", "description": "Optional: pick a drawing tab when several exist."},
                "format": {"type": "string", "enum": ["PDF", "DXF", "DWG"], "default": "PDF"},
            },
        },
    ),
]


def _ref_id(entry: dict, flat: str, nested: str) -> str:
    value = entry.get(flat)
    if not value and isinstance(entry.get(nested), dict):
        value = entry[nested].get("id")
    return str(value or "")


def parse_where_used(data: Any) -> list[dict]:
    """Drawings (one per drawing tab, newest/released preferred) from a /productstructure/whereused answer."""
    if not isinstance(data, dict):
        return []
    entries = [e for e in (data.get("items") or []) if isinstance(e, dict)]
    best: dict[tuple, dict] = {}
    for e in entries:
        etype = e.get("elementType")
        if etype not in (None, DRAWING, "DRAWING"):
            continue
        doc, ver = _ref_id(e, "documentId", "document"), _ref_id(e, "versionId", "version")
        el = str(e.get("elementId") or "")
        if not (HEX24.match(doc) and HEX24.match(ver) and HEX24.match(el)):
            continue
        revision = e.get("revision")
        if isinstance(revision, dict):
            revision = revision.get("revision") or revision.get("name")
        candidate = {
            "documentId": doc, "versionId": ver, "elementId": el,
            "name": str(e.get("elementName") or e.get("name") or e.get("partName") or "")[:120],
            "partNumber": str(e.get("partNumber") or "")[:64] or None,
            "revision": str(revision)[:16] if revision else None,
            "obsolete": bool(e.get("revisionObsolete")),
            "createdAt": str(e.get("createdAt") or ""),
        }
        rank = (bool(candidate["revision"]) and not candidate["obsolete"], candidate["createdAt"])
        current = best.get((doc, el))
        if current is None or rank > current["_rank"]:
            best[(doc, el)] = {**candidate, "_rank": rank}
    out = []
    for item in best.values():
        item.pop("_rank")
        out.append(item)
    return out


class NotFound(PolicyDenied):
    pass


class ExtraTools:
    def __init__(
        self,
        client,
        check_read: Callable[[str | None], Awaitable[None]],
        get_bytes: Callable[[str, int], Awaitable[bytes]] | None = None,
        kv: KVCache | None = None,
        files: FileCache | None = None,
        links: DownloadLinks | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        mapping: MappingStore | None = None,
        web_base_url: str = "https://cad.onshape.com",
    ):
        self.client = client
        self.check_read = check_read
        self.get_bytes = get_bytes
        self.kv = kv
        self.files = files
        self.links = links
        self.sleep = sleep
        self.mapping = mapping
        self.web_base_url = web_base_url.rstrip("/")
        self._company_id: str | None = None

    def _url(self, did: str | None, vid: str | None, eid: str | None) -> str | None:
        """Link to the released version in Onshape - built from stored IDs, costs no API call."""
        if all(HEX24.match(str(x or "")) for x in (did, vid, eid)):
            return f"{self.web_base_url}/documents/{did}/v/{vid}/e/{eid}"
        return None

    # ------------------------------------------------------------------ lookups

    async def _company(self) -> str:
        if self._company_id is None and self.mapping is not None:
            self._company_id = self.mapping.meta("company_id")
        if self._company_id is None and self.kv is not None:
            self._company_id = self.kv.get("company_id")
        if self._company_id is None:
            data = await self.client.get("/api/companies")
            items = data.get("items", []) if isinstance(data, dict) else []
            if not items:
                raise PolicyDenied("Part-number lookup needs an Onshape company account (release management).")
            self._company_id = str(items[0]["id"])
            if self.mapping is not None:
                self.mapping.set_meta("company_id", self._company_id)
            elif self.kv is not None:
                self.kv.set("company_id", self._company_id)
        return self._company_id

    async def _revision(self, part_number: str, revision: str | None, element_type: int | None = None) -> dict:
        part_number = (part_number or "").strip()
        if not PART_NUMBER_RE.match(part_number) or ".." in part_number:
            raise PolicyDenied("'partNumber' has an unexpected format.")
        if revision and not REVISION_RE.match(revision):
            raise PolicyDenied("'revision' has an unexpected format.")
        if self.mapping is not None:  # persistent mapping table
            cached = self.mapping.get_revision(part_number, revision, element_type)
            if cached is None:
                cached = await self._fetch_revision(part_number, revision, element_type)
                self.mapping.put_revision(part_number, revision, element_type, cached)
        else:
            key = f"rev2:{part_number}|{revision or ''}|{'' if element_type is None else element_type}"
            cached = self.kv.get(key) if self.kv is not None else None
            if cached is None:
                cached = await self._fetch_revision(part_number, revision, element_type)
                if self.kv is not None:
                    ttl = NOT_FOUND_TTL if cached.get("notFound") else (REVISION_TTL_FIXED if revision else REVISION_TTL_LATEST)
                    self.kv.set(key, cached, ttl)
        if cached.get("notFound"):
            kind = f" {ELEMENT_NAMES.get(element_type, '').lower()}" if element_type is not None else ""
            raise NotFound(
                f"No released{kind} item with number {part_number}. Only released items (with a revision) "
                "can be found by number."
            )
        await self.check_read(cached.get("documentId"))
        return cached

    async def _fetch_revision(self, part_number: str, revision: str | None, element_type: int | None) -> dict:
        cid = await self._company()
        params: dict[str, Any] = {}
        if revision:
            params["revision"] = revision
        if element_type is not None:
            params["elementType"] = element_type
        try:
            info = await self.client.get(
                f"/api/revisions/c/{quote(cid, safe='')}/partnumber/{quote(part_number, safe='')}",
                params=params or None,
            )
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                return {"notFound": True}
            raise
        if not isinstance(info, dict) or not info.get("documentId"):
            return {"notFound": True}
        keep = (
            "partNumber", "name", "revision", "isObsolete", "documentName", "versionName", "releaseName",
            "releaseCreatedDate", "description", "elementType", "documentId", "versionId", "elementId",
            "partId", "configuration", "releaseId",
        )
        info = {k: info.get(k) for k in keep}
        for k in ("documentId", "versionId", "elementId"):
            if not HEX24.match(str(info.get(k) or "")):
                return {"notFound": True}
        return info

    async def _model_revision(self, part_number: str, revision: str | None) -> dict:
        """Part studio or assembly revision for a number (1 call; a drawing with the same number is skipped)."""
        info = await self._revision(part_number, revision)
        if info.get("elementType") in (PARTSTUDIO, ASSEMBLY):
            return info
        try:
            return await self._revision(part_number, revision, PARTSTUDIO)
        except NotFound:
            return await self._revision(part_number, revision, ASSEMBLY)

    # ------------------------------------------------------------------ tools

    def _ws_url(self, hit: dict) -> str | None:
        ids = (hit.get("documentId"), hit.get("workspaceId"), hit.get("elementId"))
        if all(HEX24.match(str(x or "")) for x in ids):
            return f"{self.web_base_url}/documents/{ids[0]}/w/{ids[1]}/e/{ids[2]}"
        return None

    async def _index_document(self, did: str) -> None:
        """Index all part numbers of a document's main workspace: 1 call (+1 once per document)."""
        await self.check_read(did)
        state = self.mapping.doc_state(did)
        wid = state["workspaceId"] if state else None
        if not wid:
            info = await self.client.get(f"/api/v6/documents/{did}")
            wid = str(((info or {}).get("defaultWorkspace") or {}).get("id") or "")
            if not HEX24.match(wid):
                raise PolicyDenied("Could not determine the main workspace of the document.")
            self.mapping.set_doc_workspace(did, wid, str(info.get("name") or "")[:120])
        parts = await self.client.get(f"/api/parts/d/{did}/w/{wid}")
        rows = []
        for p in parts if isinstance(parts, list) else []:
            pn = str(p.get("partNumber") or "").strip()
            eid, pid = str(p.get("elementId") or ""), str(p.get("partId") or "")
            if len(pn) <= 64 and HEX24.match(eid) and PATH_SAFE_ID_RE.match(pid):  # also unnumbered (name search)
                rows.append({"partNumber": pn, "elementId": eid, "partId": pid,
                             "name": str(p.get("name") or "")[:120], "state": p.get("state"),
                             "revision": p.get("revision")})
        self.mapping.replace_ws_parts(did, wid, rows)
        logger.info(f"indexed document parts={len(rows)}")

    async def _find_unreleased(self, part_number: str, document_id: str | None = None) -> list[dict]:
        """Part numbers that were never released: look them up in the workspace index."""
        if self.mapping is None:
            return []
        if document_id:
            if not self.mapping.doc_index_fresh(document_id):
                await self._index_document(document_id)
            return [h for h in self.mapping.find_ws_part(part_number) if h["documentId"] == document_id]
        hits = self.mapping.find_ws_part(part_number)
        fresh = [h for h in hits if self.mapping.doc_index_fresh(h["documentId"])]
        if fresh:
            return fresh
        located = await self._locate_by_where_used(part_number)
        if located:  # company-wide: document that contains the part -> index it once
            if not self.mapping.doc_index_fresh(located):
                await self._index_document(located)
            hits = self.mapping.find_ws_part(part_number)
            if hits:
                return hits
        order = [h["documentId"] for h in hits] + self.mapping.known_documents()
        todo = [d for d in dict.fromkeys(order) if not self.mapping.doc_index_fresh(d)]
        for did in todo[:MAX_INDEX_DOCS_PER_LOOKUP]:
            await self._index_document(did)
            hits = self.mapping.find_ws_part(part_number)
            if hits:
                break
        return hits

    async def _locate_by_where_used(self, part_number: str) -> str | None:
        """1 call: Onshape Where Used by part number (company-wide) -> document containing the part.

        Works for every part that is used in at least one assembly; misses are remembered for 1 h.
        """
        miss_key = f"wu_pn_miss:{part_number}"
        if self.kv is not None and self.kv.get(miss_key):
            return None
        status = 200
        try:
            data = await self.client.get(
                "/api/v17/productstructure/whereused",
                params={"partNumber": part_number, "includeProperties": "false", "useLatestVersion": "false"},
            )
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in (400, 403, 404):
                raise
            data = {}
        items = (data.get("items") or []) if isinstance(data, dict) else []
        logger.info(f"whereused by partNumber: status={status} items={len(items)}")
        for it in items:
            did = str(it.get("peerDocumentId") or "")
            if str(it.get("peerPartNumber") or "") == part_number and HEX24.match(did):
                return did
        if self.kv is not None:
            self.kv.set(miss_key, True, NOT_FOUND_TTL)
        return None

    def _not_found(self, part_number: str) -> PolicyDenied:
        docs = []
        if self.mapping is not None:
            for did in self.mapping.known_documents()[:10]:
                st = self.mapping.doc_state(did)
                if st and st.get("name"):
                    docs.append(st["name"])
        where = f" Searched: {', '.join(docs)}." if docs else ""
        return NotFound(
            f"{part_number} is neither released nor in the indexed documents.{where} STOP - do not browse or "
            "search Onshape for it (expensive). Ask the user which document contains it, then call again with "
            "documentId."
        )

    async def find_part_by_number(self, args: dict) -> list:
        part_number = (args.get("partNumber") or "").strip()
        try:
            info = await self._revision(part_number, args.get("revision"))
        except NotFound:
            if args.get("revision"):
                raise
            hits = await self._find_unreleased(part_number, args.get("documentId"))
            if not hits:
                raise self._not_found(part_number) from None
            out = {
                "partNumber": part_number,
                "released": False,
                "note": "NOT released - this is the current working state (workspace), not a released version.",
                "matches": [
                    {"name": h["name"], "documentName": h.get("documentName"), "state": h.get("state"),
                     "onshapeUrl": self._ws_url(h), "documentId": h["documentId"], "wvm": "w",
                     "wvmId": h["workspaceId"], "elementId": h["elementId"], "partId": h["partId"]}
                    for h in hits[:5]
                ],
            }
            return [TextContent(type="text", text=json.dumps(out, ensure_ascii=False, separators=(",", ":")))]
        out = {k: v for k, v in info.items() if v not in (None, "")}
        out["elementType"] = ELEMENT_NAMES.get(info.get("elementType"), info.get("elementType"))
        out["released"] = True
        out["onshapeUrl"] = self._url(info.get("documentId"), info.get("versionId"), info.get("elementId"))
        out["wvm"] = "v"
        out["wvmId"] = out.pop("versionId", None)
        return [TextContent(type="text", text=json.dumps(out, ensure_ascii=False, separators=(",", ":")))]

    async def get_parts(self, args: dict) -> list:
        """Replaces the upstream get_parts: same single call, but includes part numbers, compact output."""
        did, wid, eid = args.get("documentId"), args.get("workspaceId"), args.get("elementId")
        await self.check_read(did)
        parts = await self.client.get(f"/api/parts/d/{did}/w/{wid}/e/{eid}")
        rows = [
            {k: v for k, v in {
                "name": str(p.get("name") or "")[:120], "partId": p.get("partId"),
                "partNumber": p.get("partNumber") or None, "revision": p.get("revision") or None,
                "state": p.get("state") or None,
            }.items() if v is not None}
            for p in (parts if isinstance(parts, list) else [])
        ]
        return [TextContent(type="text", text=json.dumps(rows, ensure_ascii=False, separators=(",", ":")))]

    async def find_drawing_for_part(self, args: dict) -> list:
        part = await self._model_revision(args.get("partNumber", ""), args.get("revision"))
        found = await self._drawings_where_used(part)
        out = {
            "part": f"{part.get('partNumber')} Rev {part.get('revision')} {part.get('name') or ''}".strip(),
            "partUrl": self._url(part.get("documentId"), part.get("versionId"), part.get("elementId")),
            "drawings": [
                {"drawingNumber": d.get("partNumber"), "revision": d.get("revision"), "name": d.get("name"),
                 "released": bool(d.get("revision")),
                 "onshapeUrl": self._url(d.get("documentId"), d.get("versionId"), d.get("elementId"))}
                for d in found
            ],
        }
        return [TextContent(type="text", text=json.dumps(out, ensure_ascii=False, separators=(",", ":")))]

    async def search_part_index(self, args: dict) -> list:
        if self.mapping is None:
            raise PolicyDenied("Part index is not available.")
        text = (args.get("text") or "").strip()
        if not (2 <= len(text) <= 64) or any(c in text for c in "%_\\"):
            raise PolicyDenied("'text' must be 2-64 characters (no % or _).")
        hits = self.mapping.search_ws_parts(text, 20)
        out = [
            {"partNumber": h["partNumber"], "name": h["name"], "documentName": h.get("documentName"),
             "state": h.get("state"), "onshapeUrl": self._ws_url(h)}
            for h in hits
        ]
        return [TextContent(type="text", text=json.dumps(out, ensure_ascii=False, separators=(",", ":")))]

    async def export_mapping_table(self, args: dict) -> list:
        """Whole mapping table as CSV download (Excel-friendly) - no Onshape call."""
        if self.mapping is None or self.links is None or self.files is None:
            raise PolicyDenied("Export is not available.")
        import csv
        import io
        import time as _time

        rows = self.mapping.export_rows()
        buf = io.StringIO()
        cols = ["Teilenummer", "Revision", "Typ", "Status", "Aktuellste Revision", "Name", "Dokument", "Freigabe",
                "Zeichnung", "Onshape-Link"]
        writer = csv.writer(buf, delimiter=";")
        writer.writerow(cols)
        for r in rows:
            link = (self._url(r["documentId"], r["wvmId"], r["elementId"]) if r["wvm"] == "v"
                    else self._ws_url({"documentId": r["documentId"], "workspaceId": r["wvmId"],
                                       "elementId": r["elementId"]})) or ""
            writer.writerow([r[c] for c in cols[:-1]] + [link])
        data = ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM: Excel opens umlauts correctly
        stamp = _time.strftime("%Y-%m-%d")
        path = self.files.put(f"mapping-export:{_time.time()}", data)
        url = self.links.create(path, f"Onshape-Zuordnungstabelle_{stamp}.csv", "text/csv")
        released = sum(1 for r in rows if r["Status"].startswith("freigegeben"))
        return [TextContent(type="text", text=(
            f"[Onshape-Zuordnungstabelle_{stamp}.csv]({url}) - {len(rows)} Zeilen "
            f"({released} Freigaben, {len(rows) - released} nicht freigegebene Teile). Link valid 24 h."
        ))]

    async def show_mapping_table(self, args: dict) -> list:
        if self.mapping is None:
            raise PolicyDenied("Mapping table is not available.")
        pattern = (args.get("partNumber") or "").strip() or None
        if pattern and not re.fullmatch(r"[A-Za-z0-9 _.#+\-*]{1,64}", pattern):
            raise PolicyDenied("'partNumber' pattern has an unexpected format.")
        limit = max(1, min(int(args.get("limit") or 50), 200))
        out = {"summary": self.mapping.summary(), "rows": self.mapping.rows(pattern, limit)}
        return [TextContent(type="text", text=json.dumps(out, ensure_ascii=False, separators=(",", ":")))]

    async def capture_part_screenshot(self, args: dict) -> list:
        configuration = None
        kind = "parts"
        info = None
        unreleased_label = None
        if args.get("partNumber"):
            try:
                info = await self._model_revision(args["partNumber"], args.get("revision"))
            except NotFound:
                if args.get("revision"):
                    raise
                hits = await self._find_unreleased(args["partNumber"].strip(), args.get("documentId"))
                if len(hits) != 1:
                    raise (self._not_found(args["partNumber"]) if not hits else PolicyDenied(
                        "Part number exists several times in the working state - use find_part_by_number "
                        "and pass the IDs.")) from None
                h = hits[0]
                unreleased_label = f"{args['partNumber'].strip()} {h.get('name') or ''} (NOT released, working state)"
                args = {**args, "partNumber": None, "documentId": h["documentId"], "wvm": "w",
                        "wvmId": h["workspaceId"], "elementId": h["elementId"], "partId": h["partId"]}
        if info is not None:
            did, wvm, wvmid, eid = info["documentId"], "v", info["versionId"], info["elementId"]
            pid = info.get("partId")
            configuration = info.get("configuration") or None
            if info.get("elementType") == ASSEMBLY:
                kind, pid = "assemblies", None
            label = f"{info.get('partNumber')} Rev {info.get('revision')} {info.get('name') or ''}".strip()
        else:
            did, wvm, wvmid = args.get("documentId"), args.get("wvm", "w"), args.get("wvmId")
            eid, pid = args.get("elementId"), args.get("partId")
            if not all([did, wvmid, eid, pid]):
                raise PolicyDenied("Give either partNumber or documentId, wvm, wvmId, elementId and partId.")
            if wvm not in {"w", "v", "m"}:
                raise PolicyDenied("'wvm' must be w, v or m.")
            await self.check_read(did)
            label = unreleased_label or f"Part {pid}"
        for value in (did, wvmid, eid):
            if not HEX24.match(str(value or "")):
                raise PolicyDenied("Unexpected Onshape ID.")
        if kind == "parts" and not PATH_SAFE_ID_RE.match(str(pid or "")):
            raise PolicyDenied("'partId' is not a valid part ID.")

        view = args.get("view") or AUTO_VIEW_MATRIX
        if view.lower() in NAMED:
            matrix = view.lower()
        elif view.lower() in {"iso", "isometric"}:
            matrix = ISO_VIEW
        else:
            matrix = view  # validated as 12 numbers by sanitize_arguments
        width, height = int(args.get("outputWidth", 1000)), int(args.get("outputHeight", 750))

        cache_key = f"shot:{did}:{wvmid}:{eid}:{pid}:{matrix}:{width}x{height}:{configuration or ''}"
        cacheable = wvm == "v" and self.files is not None  # versions are immutable
        if cacheable and (hit := self.files.get(cache_key)):
            with open(hit, "rb") as fh:
                data = base64.b64encode(fh.read()).decode()
            return [ImageContent(type="image", data=data, mimeType="image/png"),
                    TextContent(type="text", text=f"Screenshot: {label} (cached, 0 API calls)")]

        params: dict[str, Any] = {
            "viewMatrix": matrix, "outputWidth": width, "outputHeight": height,
            "pixelSize": 0, "edges": "show", "useAntiAliasing": True,
        }
        if configuration:
            params["configuration"] = configuration
        if kind == "parts":
            path = f"/api/parts/d/{did}/{wvm}/{wvmid}/e/{eid}/partid/{quote(pid, safe='')}/shadedviews"
        else:
            path = f"/api/assemblies/d/{did}/{wvm}/{wvmid}/e/{eid}/shadedviews"
        data = await self.client.get(path, params=params)
        images = data.get("images") or []
        first = images[0] if images else None
        if isinstance(first, list):
            first = first[0] if first else None
        if not first:
            raise PolicyDenied("Onshape returned no image.")
        raw = base64.b64decode(first, validate=True)
        if cacheable:
            self.files.put(cache_key, raw)
        return [ImageContent(type="image", data=first, mimeType="image/png"),
                TextContent(type="text", text=f"Screenshot: {label}")]

    async def export_released_model(self, args: dict) -> list:
        info = await self._model_revision(args.get("partNumber", ""), args.get("revision"))
        did, vid, eid = info["documentId"], info["versionId"], info["elementId"]
        body: dict[str, Any] = {"formatName": "STEP", "storeInDocument": False}
        if info.get("elementType") == ASSEMBLY:
            kind, part_id = "assemblies", None
        else:
            kind, part_id = "partstudios", info.get("partId")
            if part_id:
                if not PATH_SAFE_ID_RE.match(str(part_id)):
                    raise PolicyDenied("Unexpected part ID.")
                body["partIds"] = part_id
        if info.get("configuration"):
            body["configuration"] = info["configuration"]
        filename = f"{info.get('partNumber')}_Rev{info.get('revision')}.step"
        key = f"export:{did}:{vid}:{eid}:{part_id or '-'}:STEP:{info.get('configuration') or ''}"
        return await self._export(key, f"/api/{kind}/d/{did}/v/{vid}/e/{eid}/translations", did, body,
                                  filename, "application/step", info)

    async def export_released_drawing(self, args: dict) -> list:
        fmt = (args.get("format") or "PDF").upper()
        if fmt not in DRAWING_FORMATS:
            raise PolicyDenied("format must be PDF, DXF or DWG.")
        ext, media = DRAWING_FORMATS[fmt]
        did, vid, eid, label, released, info = await self._drawing_target(args)
        body = {"formatName": fmt, "storeInDocument": False, "showOverriddenDimensions": True}
        if fmt == "PDF":
            body["selectablePdfText"] = True
        key = f"export:{did}:{vid}:{eid}:-:{fmt}:"
        result = await self._export(key, f"/api/drawings/d/{did}/v/{vid}/e/{eid}/translations", did, body,
                                    f"{label}.{ext}", media, info)
        if not released:
            result.append(TextContent(
                type="text",
                text="Note: the drawing itself is not released; exported as it was in the part's release version.",
            ))
        return result

    async def _drawing_target(self, args: dict):
        rev = args.get("revision")
        if args.get("drawingNumber"):
            info = await self._revision(args["drawingNumber"], rev, DRAWING)
            label = f"{info.get('partNumber')}_Rev{info.get('revision')}"
            return info["documentId"], info["versionId"], info["elementId"], label, True, info
        part_number = args.get("partNumber")
        if not part_number:
            raise PolicyDenied("Give partNumber or drawingNumber.")
        part = await self._model_revision(part_number, rev)
        wanted = args.get("drawingElementId")

        # 1) Where Used of the released part version (what the Onshape UI shows) - 1 call, cached 1 h
        found = await self._drawings_where_used(part)
        if wanted:
            found = [d for d in found if d["elementId"] == wanted]
        if len(found) == 1:
            d = found[0]
            await self.check_read(d["documentId"])
            number = d.get("partNumber") or f"{part.get('partNumber')}_drawing"
            label = f"{number}_Rev{d['revision']}" if d.get("revision") else number
            info = {**part, "partNumber": number, "revision": d.get("revision") or "-", "name": d.get("name")}
            return d["documentId"], d["versionId"], d["elementId"], label, bool(d.get("revision")), info
        if len(found) > 1:
            options = "; ".join(
                f"{d.get('partNumber') or d.get('name') or 'drawing'} Rev {d.get('revision') or '-'} "
                f"(drawingNumber={d['partNumber']})" if d.get("partNumber") else
                f"{d.get('name') or 'drawing'} (drawingElementId={d['elementId']})"
                for d in found[:10]
            )
            raise PolicyDenied(f"Several drawings use {part_number}, call again with one of: {options}")

        # 2) fallbacks for setups without Where Used results
        label = f"{part.get('partNumber')}_Rev{part.get('revision')}_drawing"
        if not wanted:
            in_package = await self._drawings_in_release(part)
            if len(in_package) == 1:
                d = in_package[0]
                await self.check_read(d["documentId"])
                return d["documentId"], d["versionId"], d["elementId"], label, True, part
        did, vid = part["documentId"], part["versionId"]
        drawings = await self._drawings_in_version(did, vid)
        if wanted:
            match = [d for d in drawings if d["id"] == wanted]
        elif len(drawings) == 1:
            match = drawings
        else:
            needles = [n.lower() for n in (part.get("partNumber"), part.get("name")) if n]
            match = [d for d in drawings if any(n in d["name"].lower() for n in needles)]
        if len(match) != 1:
            if not drawings:
                raise PolicyDenied(
                    f"No drawing found that uses {part_number}. If you know the drawing number, call again "
                    "with drawingNumber."
                )
            options = ", ".join(f"{d['name']} (drawingElementId={d['id']})" for d in drawings[:15])
            raise PolicyDenied(f"Several drawings found, call again with drawingElementId: {options}")
        return did, vid, match[0]["id"], label, False, part

    async def _drawings_where_used(self, part: dict) -> list[dict]:
        did, vid, eid = part["documentId"], part["versionId"], part["elementId"]
        pid = part.get("partId") or ""
        key = f"whereused5:{did}:{vid}:{eid}:{pid}:{part.get('configuration') or ''}"
        if self.mapping is not None:
            cached = self.mapping.get_drawings(str(part.get("partNumber")), str(part.get("revision")))
        else:
            cached = self.kv.get(key) if self.kv is not None else None
        if cached is not None:
            return cached
        base_params: dict[str, Any] = {
            "documentId": did, "elementId": eid, "versionId": vid, "limitToTypes": "2",
        }
        if pid:
            base_params["partId"] = pid
        if part.get("configuration"):
            base_params["configuration"] = part["configuration"]

        async def where_used(extra: dict) -> list[dict]:
            try:
                data = await self.client.get("/api/productstructure/whereused", params={**base_params, **extra})
            except Exception as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) in (400, 403, 404):
                    return []
                raise
            return parse_where_used(data)

        # filter 0 = LATEST_REVISION: released drawings incl. number + revision -> no extra lookup needed
        candidates = await where_used({"filter": 0})
        if not candidates:  # no released drawing: look for drawings in plain versions
            candidates = await where_used({})
        result, direct = [], 0
        for cand in candidates[:5]:
            if cand.get("revision") and cand.get("partNumber"):
                result.append(cand)
                direct += 1
            else:
                result.append(await self._released_drawing(cand))
        logger.info(f"whereused drawings={len(candidates)} resolved_directly={direct}")
        if self.mapping is not None:
            self.mapping.put_drawings(str(part.get("partNumber")), str(part.get("revision")), result)
        elif self.kv is not None:
            self.kv.set(key, result, REVISION_TTL_LATEST)  # new drawing revisions may appear
        return result

    async def _released_drawing(self, cand: dict) -> dict:
        """Latest released revision of a drawing tab (its own number, e.g. DRW-000635) - 1 call."""
        if self.mapping is not None:  # already known from the revision sync: no call
            known = self.mapping.latest_drawing_for_element(cand["documentId"], cand["elementId"])
            if known and HEX24.match(str(known.get("versionId") or "")):
                return {
                    **cand, "versionId": known["versionId"],
                    "partNumber": str(known.get("partNumber") or "")[:64] or None,
                    "revision": str(known.get("revision") or "")[:16] or None,
                    "name": str(known.get("name") or cand.get("name") or "")[:120],
                }
        cid = await self._company()
        try:
            hist = await self.client.get(
                f"/api/revisions/companies/{quote(cid, safe='')}/d/{cand['documentId']}/v/{cand['versionId']}"
                f"/e/{cand['elementId']}",
                params={"elementType": DRAWING},
            )
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) in (400, 403, 404):
                return cand
            raise
        items = hist.get("items") if isinstance(hist, dict) else hist
        released = [
            r for r in (items or [])
            if isinstance(r, dict) and not r.get("isObsolete") and r.get("revision")
            and HEX24.match(str(r.get("versionId") or "")) and HEX24.match(str(r.get("documentId") or ""))
            and HEX24.match(str(r.get("elementId") or ""))
        ]
        if not released:
            return cand
        latest = max(released, key=lambda r: str(r.get("releaseCreatedDate") or r.get("createdAt") or ""))
        return {
            **cand,
            "documentId": latest["documentId"], "versionId": latest["versionId"], "elementId": latest["elementId"],
            "partNumber": str(latest.get("partNumber") or "")[:64] or None,
            "revision": str(latest.get("revision"))[:16],
            "name": str(latest.get("name") or cand.get("name") or "")[:120],
        }

    async def _drawings_in_release(self, part: dict) -> list[dict]:
        rpid = str(part.get("releaseId") or "")
        if not HEX24.match(rpid):
            return []
        key = f"releasedrawings:{rpid}:{part.get('partId') or ''}"
        cached = self.kv.get(key) if self.kv is not None else None
        if cached is None:
            try:
                package = await self.client.get(f"/api/releasepackages/{rpid}")
            except Exception as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) in (403, 404):
                    package = {}
                else:
                    raise
            items = package.get("items") or [] if isinstance(package, dict) else []
            part_item_ids = {
                i.get("id") for i in items
                if isinstance(i, dict) and i.get("partId") == part.get("partId")
                and i.get("elementId") == part.get("elementId")
            }
            drawings = []
            for i in items:
                if not isinstance(i, dict) or i.get("elementType") not in (DRAWING, "DRAWING"):
                    continue
                ids = {k: str(i.get(k) or "") for k in ("documentId", "versionId", "elementId")}
                if all(HEX24.match(v) for v in ids.values()):
                    refs = set(i.get("referenceIds") or [])
                    drawings.append({**ids, "refersToPart": bool(refs & part_item_ids)})
            referring = [d for d in drawings if d["refersToPart"]]
            cached = referring or drawings
            if self.kv is not None:
                self.kv.set(key, cached)  # a released package never changes
        return cached

    async def _drawings_in_version(self, did: str, vid: str) -> list[dict]:
        key = f"drawings:{did}:{vid}"
        cached = self.kv.get(key) if self.kv is not None else None
        if cached is None:
            data = await self.client.get(f"/api/documents/d/{did}/v/{vid}/elements", params={"elementType": "DRAWING"})
            items = data if isinstance(data, list) else (data.get("items") or [])
            cached = [
                {"id": e["id"], "name": str(e.get("name", ""))[:120]}
                for e in items
                if isinstance(e, dict) and HEX24.match(str(e.get("id", "")))
                and str(e.get("elementType", "DRAWING")).upper() == "DRAWING"
            ]
            if self.kv is not None:
                self.kv.set(key, cached)  # a version never changes
        return cached

    # ------------------------------------------------------------------ export engine

    async def _export(self, key: str, post_path: str, did: str, body: dict, filename: str,
                      media_type: str, info: dict) -> list:
        if self.files is None or self.links is None or self.get_bytes is None:
            raise PolicyDenied("Exports need the download store (IMAGE_LINKS with PUBLIC_BASE_URL).")
        async with self.files.lock(key):  # identical parallel requests share one translation
            cached = self.files.get(key)
            calls_note = "cached, 0 API calls"
            if cached is None:
                pending_key = f"pending:{key}"
                pending = self.kv.get(pending_key) if self.kv is not None else None
                if pending:
                    tid, delays = pending["tid"], POLL_DELAYS_RESUME
                else:
                    started = await self.client.post(post_path, data=body)
                    tid = str(started.get("id", ""))
                    if not EXTERNAL_ID_RE.match(tid):
                        raise PolicyDenied("Onshape did not start the export.")
                    if self.kv is not None:
                        self.kv.set(pending_key, {"tid": tid}, PENDING_TTL)
                    delays = POLL_DELAYS_NEW
                status = await self._wait(tid, delays, pending_key)
                if status is None:
                    return [TextContent(
                        type="text",
                        text="Export is still running in Onshape. Call this tool again with the same arguments "
                             "in about a minute - it will pick up the running export.",
                    )]
                if self.kv is not None:
                    self.kv.delete(pending_key)
                ids = status.get("resultExternalDataIds") or []
                fid = str(ids[0]) if ids else ""
                result_doc = str(status.get("resultDocumentId") or did)
                if not EXTERNAL_ID_RE.match(fid) or not HEX24.match(result_doc):
                    raise PolicyDenied("Onshape finished the export but returned no file.")
                data = await self.get_bytes(f"/api/documents/d/{result_doc}/externaldata/{fid}", MAX_DOWNLOAD_BYTES)
                if data[:2] == b"PK" and not filename.endswith(".zip"):
                    filename, media_type = filename.rsplit(".", 1)[0] + ".zip", "application/zip"
                self.files.put(f"{key}:meta", json.dumps({"zip": media_type == "application/zip"}).encode())
                cached = self.files.put(key, data)
                calls_note = "exported now"
            else:
                meta_path = self.files.get(f"{key}:meta")
                if meta_path:  # name always follows the current lookup (e.g. drawing number)
                    with open(meta_path, encoding="utf-8") as fh:
                        meta = json.load(fh)
                    if meta.get("zip") or meta.get("media") == "application/zip":
                        filename, media_type = filename.rsplit(".", 1)[0] + ".zip", "application/zip"
        url = self.links.create(cached, filename, media_type)
        size_kb = max(1, os.path.getsize(cached) // 1024)
        title = f"{info.get('partNumber')} Rev {info.get('revision')} {info.get('name') or ''}".strip()
        return [TextContent(
            type="text",
            text=f"[{filename}]({url}) - {title}, {size_kb} KB ({calls_note}). Link valid 24 h.",
        )]

    async def _wait(self, tid: str, delays, pending_key: str) -> dict | None:
        for delay in delays:
            await self.sleep(delay)
            status = await self.client.get(f"/api/translations/{tid}")
            state = str(status.get("requestState", "")).upper()
            if state == "DONE":
                return status
            if state == "FAILED":
                if self.kv is not None:
                    self.kv.delete(pending_key)
                raise PolicyDenied(f"Onshape export failed: {str(status.get('failureReason') or '')[:200]}")
        return None

    # ------------------------------------------------------------------ batch (admin only)

    async def sync_revisions(self, max_pages: int = 50) -> dict:
        """All latest released revisions of the company: 1 call per 100 revisions, incremental."""
        if self.mapping is None:
            raise PolicyDenied("Mapping table is not available.")
        cid = await self._company()
        after = self.mapping.meta("rev_sync_after") or "2000-01-01T00:00:00Z"
        path = f"/api/revisions/companies/{quote(cid, safe='')}"
        params: dict | None = {"latestOnly": "true", "limit": 100, "after": after}
        base = self.web_base_url + "/api/"
        pages = stored = 0
        newest = after
        while path and pages < max_pages:
            data = await self.client.get(path, params=params)
            pages += 1
            for item in (data.get("items") or []) if isinstance(data, dict) else []:
                keep = {k: item.get(k) for k in (
                    "partNumber", "name", "revision", "isObsolete", "documentName", "versionName", "releaseName",
                    "releaseCreatedDate", "description", "elementType", "documentId", "versionId", "elementId",
                    "partId", "configuration", "releaseId", "createdAt")}
                if all(HEX24.match(str(keep.get(k) or "")) for k in ("documentId", "versionId", "elementId")):
                    self.mapping.put_synced_revision(keep)
                    stored += 1
                    newest = max(newest, str(keep.get("createdAt") or keep.get("releaseCreatedDate") or ""))
            nxt = str((data or {}).get("next") or "")
            path, params = (nxt[len(self.web_base_url):], None) if nxt.startswith(base) else (None, None)
        complete = path is None
        if complete:
            self.mapping.set_meta("rev_sync_after", newest)
        return {"pages": pages, "revisionsStored": stored, "complete": complete}

    async def link_drawings(self, document_id: str | None, max_parts: int) -> dict:
        """Where Used per released part (1 call each) -> part->drawing links; drawing data from the sync."""
        if self.mapping is None:
            raise PolicyDenied("Mapping table is not available.")
        todo = [
            p for p in self.mapping.released_parts(document_id)
            if self.mapping.get_drawings(str(p.get("partNumber")), str(p.get("revision"))) is None
        ]
        linked = 0
        for part in todo[:max_parts]:
            found = await self._drawings_where_used(part)
            linked += bool(found)
        return {"partsChecked": min(len(todo), max_parts), "partsWithDrawing": linked,
                "remaining": max(0, len(todo) - max_parts)}

    def handler(self, name: str):
        return {
            "find_part_by_number": self.find_part_by_number,
            "get_parts": self.get_parts,
            "find_drawing_for_part": self.find_drawing_for_part,
            "show_mapping_table": self.show_mapping_table,
            "export_mapping_table": self.export_mapping_table,
            "search_part_index": self.search_part_index,
            "capture_part_screenshot": self.capture_part_screenshot,
            "export_released_model": self.export_released_model,
            "export_released_drawing": self.export_released_drawing,
        }[name]
