"""Persistent mapping table (SQLite on the Fly volume) that replaces repeated Onshape lookups.

Tables
  revisions  part number + revision + element type -> Onshape IDs   (a released revision never changes)
  latest     part number (+ requested type) -> latest revision      (invalidated by release webhook)
  drawings   part number + part revision -> drawing(s) incl. number  (invalidated by drawing releases)
  meta       company id, webhook id/secret, timestamps
  ws_docs    documents whose working state is indexed (main workspace, freshness)
  ws_parts   part number -> part in the main workspace, also for parts that were never released

Without an active release webhook, "latest" pointers and drawing links fall back to a short TTL.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any

FALLBACK_TTL = 3600  # without webhook: re-check latest revision / drawings after 1 h
SAFETY_TTL = 30 * 86400  # with webhook: still re-check once a month
NOT_FOUND_TTL = 3600
WEBHOOK_HEALTH_MAX_AGE = 8 * 86400
INDEX_VERSION = "2"  # v2: unnumbered parts are indexed too (name search)  # webhook must have been verified within this time


class MappingStore:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS revisions (
                part_number TEXT NOT NULL, revision TEXT NOT NULL, element_type INTEGER NOT NULL,
                info TEXT NOT NULL, fetched_at REAL NOT NULL,
                PRIMARY KEY (part_number, revision, element_type));
            CREATE TABLE IF NOT EXISTS latest (
                part_number TEXT NOT NULL, type_key TEXT NOT NULL, revision TEXT, element_type INTEGER,
                checked_at REAL NOT NULL, PRIMARY KEY (part_number, type_key));
            CREATE TABLE IF NOT EXISTS drawings (
                part_number TEXT NOT NULL, part_revision TEXT NOT NULL, drawings TEXT NOT NULL,
                checked_at REAL NOT NULL, PRIMARY KEY (part_number, part_revision));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ws_docs (
                document_id TEXT PRIMARY KEY, workspace_id TEXT, name TEXT,
                indexed_at REAL NOT NULL DEFAULT 0, dirty INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS ws_parts (
                part_number TEXT NOT NULL, document_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                element_id TEXT NOT NULL, part_id TEXT NOT NULL, name TEXT, state TEXT, revision TEXT,
                PRIMARY KEY (part_number, document_id, element_id, part_id));
            """
        )

        if self.meta("index_version") != INDEX_VERSION:  # index format changed: rebuild lazily
            self._q("UPDATE ws_docs SET dirty=1")
            self.set_meta("index_version", INDEX_VERSION)

    # ---------------------------------------------------------------- helpers

    def _q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def meta(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._q("DELETE FROM meta WHERE key=?", (key,))
        else:
            self._q("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def webhook_secret(self) -> str:
        secret = self.meta("webhook_secret")
        if not secret:
            secret = secrets.token_urlsafe(32)
            self.set_meta("webhook_secret", secret)
        return secret

    def webhook_active(self) -> bool:
        verified = self.meta("webhook_verified_at")
        return bool(self.meta("webhook_id")) and bool(verified) and time.time() - float(verified) < WEBHOOK_HEALTH_MAX_AGE

    def _latest_ttl(self) -> float:
        return SAFETY_TTL if self.webhook_active() else FALLBACK_TTL

    # ---------------------------------------------------------------- revisions

    @staticmethod
    def _type_key(element_type: int | None) -> str:
        return "" if element_type is None else str(element_type)

    def get_revision(self, part_number: str, revision: str | None, element_type: int | None) -> dict | None:
        """Cached revision info, {"notFound": True}, or None (= ask Onshape)."""
        now = time.time()
        if not revision:
            rows = self._q(
                "SELECT revision, element_type, checked_at FROM latest WHERE part_number=? AND type_key=?",
                (part_number, self._type_key(element_type)),
            )
            if not rows:
                return None
            rev, etype, checked = rows[0]
            if rev is None:
                return {"notFound": True} if now - checked < NOT_FOUND_TTL else None
            if now - checked > self._latest_ttl():
                return None
            revision, element_type = rev, etype
        sql = "SELECT info FROM revisions WHERE part_number=? AND revision=?"
        args: tuple = (part_number, revision)
        if element_type is not None:
            sql += " AND element_type=?"
            args += (element_type,)
        rows = self._q(sql + " ORDER BY element_type LIMIT 1", args)
        return json.loads(rows[0][0]) if rows else None

    def put_revision(self, part_number: str, requested_revision: str | None, element_type: int | None, info: dict) -> None:
        now = time.time()
        if info.get("notFound"):
            if not requested_revision:
                self._q(
                    "INSERT OR REPLACE INTO latest VALUES (?, ?, NULL, NULL, ?)",
                    (part_number, self._type_key(element_type), now),
                )
            return
        rev = str(info.get("revision") or "")
        etype = int(info.get("elementType") if info.get("elementType") is not None else -1)
        self._q(
            "INSERT OR REPLACE INTO revisions VALUES (?, ?, ?, ?, ?)",
            (part_number, rev, etype, json.dumps(info), now),
        )
        if not requested_revision:
            self._q(
                "INSERT OR REPLACE INTO latest VALUES (?, ?, ?, ?, ?)",
                (part_number, self._type_key(element_type), rev, etype, now),
            )

    def put_synced_revision(self, info: dict) -> None:
        """Store one latest revision from the company-wide enumeration (also sets 'latest' pointers)."""
        pn = str(info.get("partNumber") or "")
        if not pn:
            return
        rev = str(info.get("revision") or "")
        etype = int(info.get("elementType") if info.get("elementType") is not None else -1)
        now = time.time()
        self._q("INSERT OR REPLACE INTO revisions VALUES (?, ?, ?, ?, ?)", (pn, rev, etype, json.dumps(info), now))
        self._q("INSERT OR REPLACE INTO latest VALUES (?, ?, ?, ?, ?)", (pn, str(etype), rev, etype, now))
        # untyped lookups prefer parts/assemblies over a drawing with the same number
        current = self._q("SELECT element_type FROM latest WHERE part_number=? AND type_key=''", (pn,))
        if not current or current[0][0] in (None, 2) or etype != 2:
            self._q("INSERT OR REPLACE INTO latest VALUES (?, '', ?, ?, ?)", (pn, rev, etype, now))

    def latest_drawing_for_element(self, document_id: str, element_id: str) -> dict | None:
        """Released drawing revision of a drawing tab, from synced data (no API call)."""
        best = None
        for (info,) in self._q("SELECT info FROM revisions WHERE element_type=2"):
            d = json.loads(info)
            if d.get("documentId") == document_id and d.get("elementId") == element_id and not d.get("isObsolete"):
                if best is None or str(d.get("releaseCreatedDate") or "") > str(best.get("releaseCreatedDate") or ""):
                    best = d
        return best

    def released_parts(self, document_id: str | None = None) -> list[dict]:
        """Latest released parts/assemblies (optionally of one document)."""
        out = []
        for pn, rev, etype, info in self._q(
            "SELECT r.part_number, r.revision, r.element_type, r.info FROM revisions r JOIN latest l "
            "ON l.part_number=r.part_number AND l.type_key=CAST(r.element_type AS TEXT) AND l.revision=r.revision "
            "WHERE r.element_type IN (0, 1)"
        ):
            d = json.loads(info)
            if document_id is None or d.get("documentId") == document_id:
                out.append(d)
        return out

    # ---------------------------------------------------------------- drawings

    def get_drawings(self, part_number: str, part_revision: str) -> list[dict] | None:
        rows = self._q(
            "SELECT drawings, checked_at FROM drawings WHERE part_number=? AND part_revision=?",
            (part_number, part_revision),
        )
        if not rows or time.time() - rows[0][1] > self._latest_ttl():
            return None
        return json.loads(rows[0][0])

    def put_drawings(self, part_number: str, part_revision: str, drawings: list[dict]) -> None:
        self._q(
            "INSERT OR REPLACE INTO drawings VALUES (?, ?, ?, ?)",
            (part_number, part_revision, json.dumps(drawings), time.time()),
        )

    # ---------------------------------------------------------------- invalidation (webhook)

    def on_revision_created(self, part_number: str | None, element_type: int | None) -> None:
        if element_type == 2:  # a drawing was released: links part -> drawing may have changed
            self._q("DELETE FROM drawings")
        if part_number:
            self._q("DELETE FROM latest WHERE part_number=?", (part_number,))
            self._q("DELETE FROM drawings WHERE part_number=?", (part_number,))
        else:
            self.invalidate_all()

    def invalidate_all(self) -> None:
        self._q("DELETE FROM latest")
        self._q("DELETE FROM drawings")

    # ---------------------------------------------------------------- workspace part-number index

    def known_documents(self) -> list[str]:
        """Documents worth indexing: where released parts live + everything indexed before."""
        docs = {r[0] for r in self._q("SELECT document_id FROM ws_docs")}
        for (info,) in self._q("SELECT info FROM revisions"):
            did = json.loads(info).get("documentId")
            if did:
                docs.add(did)
        return sorted(docs)

    def doc_state(self, document_id: str) -> dict | None:
        rows = self._q(
            "SELECT workspace_id, name, indexed_at, dirty FROM ws_docs WHERE document_id=?", (document_id,)
        )
        if not rows:
            return None
        wid, name, indexed_at, dirty = rows[0]
        return {"workspaceId": wid, "name": name, "indexedAt": indexed_at, "dirty": bool(dirty)}

    def doc_index_fresh(self, document_id: str) -> bool:
        st = self.doc_state(document_id)
        return bool(st and not st["dirty"] and time.time() - st["indexedAt"] < self._ws_ttl())

    def _ws_ttl(self) -> float:
        # with the metadata webhook, documents are marked dirty on change; otherwise re-index daily
        return SAFETY_TTL if self.webhook_active() else 86400

    def set_doc_workspace(self, document_id: str, workspace_id: str, name: str | None) -> None:
        self._q(
            "INSERT INTO ws_docs(document_id, workspace_id, name, indexed_at, dirty) VALUES (?, ?, ?, 0, 1) "
            "ON CONFLICT(document_id) DO UPDATE SET workspace_id=excluded.workspace_id, name=excluded.name",
            (document_id, workspace_id, name),
        )

    def replace_ws_parts(self, document_id: str, workspace_id: str, parts: list[dict]) -> None:
        with self._lock:
            self._db.execute("BEGIN")
            self._db.execute("DELETE FROM ws_parts WHERE document_id=?", (document_id,))
            self._db.executemany(
                "INSERT OR REPLACE INTO ws_parts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (p["partNumber"], document_id, workspace_id, p["elementId"], p["partId"],
                     p.get("name"), p.get("state"), p.get("revision"))
                    for p in parts
                ],
            )
            self._db.execute(
                "UPDATE ws_docs SET indexed_at=?, dirty=0 WHERE document_id=?", (time.time(), document_id)
            )
            self._db.execute("COMMIT")

    def find_ws_part(self, part_number: str) -> list[dict]:
        if not part_number:
            return []
        rows = self._q(
            "SELECT p.document_id, p.workspace_id, p.element_id, p.part_id, p.name, p.state, p.revision, d.name "
            "FROM ws_parts p LEFT JOIN ws_docs d ON d.document_id=p.document_id WHERE p.part_number=?",
            (part_number,),
        )
        return [
            {"documentId": r[0], "workspaceId": r[1], "elementId": r[2], "partId": r[3], "name": r[4],
             "state": r[5], "revision": r[6], "documentName": r[7]}
            for r in rows
        ]

    def search_ws_parts(self, text: str, limit: int = 20) -> list[dict]:
        like = f"%{text}%"
        rows = self._q(
            "SELECT p.part_number, p.name, p.document_id, p.workspace_id, p.element_id, p.part_id, p.state, d.name "
            "FROM ws_parts p LEFT JOIN ws_docs d ON d.document_id=p.document_id "
            "WHERE p.name LIKE ? OR p.part_number LIKE ? ORDER BY p.part_number LIMIT ?",
            (like, like, limit),
        )
        return [
            {"partNumber": r[0], "name": r[1], "documentId": r[2], "workspaceId": r[3], "elementId": r[4],
             "partId": r[5], "state": r[6], "documentName": r[7]}
            for r in rows
        ]

    def mark_doc_dirty(self, document_id: str | None) -> None:
        if document_id:
            self._q("UPDATE ws_docs SET dirty=1 WHERE document_id=?", (document_id,))
        else:
            self._q("UPDATE ws_docs SET dirty=1")

    # ---------------------------------------------------------------- reporting

    def summary(self) -> dict[str, Any]:
        return {
            "revisions": self._q("SELECT COUNT(*) FROM revisions")[0][0],
            "latest_pointers": self._q("SELECT COUNT(*) FROM latest WHERE revision IS NOT NULL")[0][0],
            "part_drawing_links": self._q("SELECT COUNT(*) FROM drawings")[0][0],
            "indexed_documents": self._q("SELECT COUNT(*) FROM ws_docs WHERE indexed_at > 0")[0][0],
            "indexed_part_numbers": self._q("SELECT COUNT(DISTINCT part_number) FROM ws_parts WHERE part_number<>''")[0][0],
            "webhook_active": self.webhook_active(),
        }

    def export_rows(self) -> list[dict]:
        """Whole table for export: latest releases (+ drawings) and the working-state index."""
        latest = {(pn, rev, et) for pn, rev, et in self._q(
            "SELECT part_number, revision, element_type FROM latest WHERE revision IS NOT NULL")}
        links = {(pn, rev): json.loads(d) for pn, rev, d in self._q(
            "SELECT part_number, part_revision, drawings FROM drawings")}
        out = []
        for pn, rev, etype, info in self._q(
                "SELECT part_number, revision, element_type, info FROM revisions ORDER BY part_number, revision"):
            d = json.loads(info)
            drawings = links.get((pn, rev))
            out.append({
                "Teilenummer": pn, "Revision": rev,
                "Typ": {0: "Teil", 1: "Baugruppe", 2: "Zeichnung"}.get(etype, str(etype)),
                "Status": "freigegeben" + (" (obsolet)" if d.get("isObsolete") else ""),
                "Aktuellste Revision": "ja" if (pn, rev, etype) in latest else "nein",
                "Name": d.get("name") or "", "Dokument": d.get("documentName") or "",
                "Freigabe": d.get("releaseName") or "",
                "Zeichnung": "; ".join(
                    f"{x.get('partNumber') or x.get('name') or '?'} Rev {x.get('revision') or '-'}" for x in drawings
                ) if drawings else ("keine" if drawings == [] else "nicht geprüft"),
                "documentId": d.get("documentId") or "", "wvm": "v", "wvmId": d.get("versionId") or "",
                "elementId": d.get("elementId") or "", "partId": d.get("partId") or "",
            })
        released = {r["Teilenummer"] for r in out}
        for pn, did, wid, eid, pid, name, state, rev, dname in self._q(
                "SELECT p.part_number, p.document_id, p.workspace_id, p.element_id, p.part_id, p.name, p.state, "
                "p.revision, d.name FROM ws_parts p LEFT JOIN ws_docs d ON d.document_id=p.document_id "
                "WHERE p.part_number<>'' ORDER BY p.part_number"):
            if pn in released:
                continue
            out.append({
                "Teilenummer": pn, "Revision": rev or "", "Typ": "Teil",
                "Status": "nicht freigegeben (Arbeitsstand)", "Aktuellste Revision": "",
                "Name": name or "", "Dokument": dname or "", "Freigabe": "", "Zeichnung": "",
                "documentId": did, "wvm": "w", "wvmId": wid, "elementId": eid, "partId": pid,
            })
        return out

    def rows(self, part_number: str | None = None, limit: int = 50) -> list[dict]:
        sql = (
            "SELECT r.part_number, r.revision, r.element_type, r.info, "
            "(SELECT l.revision FROM latest l WHERE l.part_number=r.part_number AND l.revision IS NOT NULL LIMIT 1), "
            "(SELECT d.drawings FROM drawings d WHERE d.part_number=r.part_number AND d.part_revision=r.revision) "
            "FROM revisions r"
        )
        args: tuple = ()
        if part_number:
            sql += " WHERE r.part_number LIKE ?"
            args = (part_number.replace("*", "%"),)
        sql += " ORDER BY r.part_number, r.revision LIMIT ?"
        out = []
        for pn, rev, etype, info, latest, drawings in self._q(sql, args + (limit,)):
            info = json.loads(info)
            out.append({
                "partNumber": pn,
                "revision": rev,
                "type": {0: "PART", 1: "ASSEMBLY", 2: "DRAWING"}.get(etype, etype),
                "latest": rev == latest,
                "name": info.get("name"),
                "document": info.get("documentName"),
                "drawings": [
                    f"{d.get('partNumber') or d.get('name')} Rev {d.get('revision') or '-'}"
                    for d in (json.loads(drawings) if drawings else [])
                ] or None,
            })
        return out
