---
title: 'H-2 + H-3: Mapping-DB ist alleiniger Eigentümer der Teile-Daten, eine Schreibfunktion pro Zeilenart'
type: 'refactor'
created: '2026-10-15'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-mapping-ownership/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `_revision`, `_drawings_where_used` und `_locate_by_where_used` (`wu_pn_miss`) legen Teile-Daten ohne Mapping-DB im KV-Cache ab, obwohl der Server seit H-7 nie ohne Mapping-DB läuft (H-2, AD-6, AD-12). `put_revision` (Lookup) und `put_synced_revision` (Sync) lösen `latest[pn,'']` unterschiedlich auf; `ws_parts` liefert nebenbei Freigabestatus/-revision (H-3, AD-6).

**Approach:** `ExtraTools` verlangt `mapping`; KV-Zweige für Teile-Daten und alle `mapping is None`-Zweige entfallen; der Where-Used-Fehltreffer kommt in eine neue additive Tabelle `whereused_misses` (TTL 1 h). In `mapping.py` je eine Schreibfunktion für Revisionszeile, Latest-Zeiger (inkl. untypisierter Regel, einmal), Not-found-Marker und Fehltreffer; Lookup und Sync delegieren dorthin. `ws_parts.state`/`revision` werden weder geschrieben noch ausgegeben. Company-ID gehört `meta` (Alt-Wert aus KV einmal übernehmen). Keine zusätzlichen Onshape-Calls, kein Neuaufbau bestehender Daten, kein `INDEX_VERSION`-Sprung. Tests je Verhalten; Spine H-2/H-3 erledigt. Kein Commit ohne Erics OK.

</frozen-after-approval>

## Implementation Notes

- `mapping.py` H-3: `_write_revision_row`, `_write_latest`, `_write_not_found` sind die einzigen Schreiber für `revisions`/`latest`; `put_revision` (Lookup) und `put_synced_revision` (Sync) delegieren. Jede Latest-Antwort setzt den typisierten Zeiger ihres Typs (neu auch beim untypisierten Lookup → spart den typisierten Folge-Call). Untypisiert: Nicht-Zeichnung gewinnt immer; Zeichnung nur, wenn der aktuelle Zeiger fehlt/Not-found/Zeichnung ist oder älter als `_latest_ttl()` (sonst würde ein veralteter Teil-Zeiger bei jeder Suche neu abgefragt). Typisierte Anfrage mit Antwort anderen Typs setzt zusätzlich den angefragten Schlüssel (wie bisher).
- „Ist aktuellste Revision“ (`rows`, `released_parts`) über `_IS_LATEST`: der neueste Zeiger (typisiert oder untypisiert) je Nummer und Typ nennt die Revision (nach Review) – deckt Alt-Zeilen ab, die nur einen untypisierten Zeiger haben; `rows()` markierte vorher bei Teil + gleichnamiger Zeichnung falsch.
- H-2: `ExtraTools(..., *, mapping)` Pflicht-Keyword; alle `mapping is None`-Zweige und die KV-Schlüssel `rev2:`/`whereused5:`/`wu_pn_miss:` entfernt (Konstanten `REVISION_TTL_*`, `NOT_FOUND_TTL` in `extra_tools.py` dadurch tot → gelöscht). Neue Tabelle `whereused_misses` (additiv, `CREATE IF NOT EXISTS`, kein `INDEX_VERSION`-Sprung), Purge abgelaufener Zeilen beim Schreiben, Release-Webhook der Nummer und `invalidate_all` leeren sie. Company-ID: `meta` → Alt-Wert aus KV einmal nach `meta` → `/api/companies`.
- AD-6: `_index_document`/`replace_ws_parts` schreiben `state`/`revision` als NULL; `find_ws_part`, `search_ws_parts`, `export_rows` lesen sie nicht mehr (Alt-Zeilen bleiben unberührt, Spalten bleiben). `get_parts` (Live-Call) behält `state`.
- Bestehende Produktionsdaten: kein Neuaufbau, keine Datenänderung; typisierte Zeiger füllen sich lazy. Verwaiste KV-Einträge laufen per TTL ab (in Produktion war die Mapping-DB immer aktiv, die Schlüssel dürften gar nicht existieren). Onshape-Calls pro Lookup unverändert oder weniger.
- Dateien: `mapping.py`, `extra_tools.py`, `cache.py` (Docstring), `tests/test_units.py` (+14), `tests/test_e2e.py` (+1), `docs/DOKUMENTATION.md`, Spine H-2/H-3. Gegenprobe: 11 der 12 neuen Unit-Tests scheitern gegen den alten Code (der zwölfte sichert unverändertes Not-found-Verhalten). 236 Tests + 544 Upstream grün (nach Review 238).


## Review Triage Log

Oneshot-Layer Blind Hunter übersprungen, weil `bmad-code-review` (4 Layer: Blind Hunter, Edge Case Hunter, Verification Gap, Acceptance Auditor) direkt folgte. Patches angewendet (headless, Auftrag aus Delegations-Session).

- `_IS_LATEST` zählt jeden Zeiger; widersprechen sich Zeiger (Alt-Daten ohne Webhook, Zeiger unter anderem angefragtem Typ), gelten zwei Revisionen als aktuell und `link_drawings` zahlt Calls für die alte (BH, ECH) — medium → patch: nur der neueste Zeiger je Nummer+Typ zählt + Test.
- Vierter `latest`-Schreiber in `put_revision` für den angefragten Schlüssel (AA) — low (Vertrag CAP-3) → patch: `_write_latest(..., requested_key)`.
- Doku versprach „Teil gewinnt immer“; Code lässt nach Invalidierung/TTL die Zeichnung zu (BH, ECH, AA) — medium (Erwartung der Nutzer) → patch: Doku präzisiert, Docstring korrigiert. Verhalten selbst vorbestehend (alter Sync/Lookup gleich) → defer (Teil-Vorrang über Invalidierung hinweg bräuchte Call oder Rückgriff auf ungeprüfte Revision).
- `invalidate_all`/`on_revision_created(None)` leeren `whereused_misses` ungetestet (VG) — medium → patch: Test erweitert.
- Not-found-Marker + Zeichnung und typisierte Anfrage mit anderem Antworttyp ungetestet (BH) — low, direkte Tests → patch.
- Doku-Satz „nur aus den Freigaben“ zu breit wegen `get_parts` (BH) — low → patch: auf Tabelle/Index beschränkt.
- AD-12 nennt Company-ID im KV, `KVCache`-Docstring ebenso (AA) — low: AD-12 erlaubt, verlangt nicht; Docstring angepasst, Spine-Regel bleibt (Spec-Edit) → kein weiterer Patch.
- CAP-1: kein Test, dass der gespeicherte Fehltreffer den zweiten Call spart (AA) — false: `test_unknown_part_number_stops_without_browsing` prüft `n + 2` nach dem zweiten Aufruf, läuft jetzt über `mappings.db`.
- `mapping=None` zur Laufzeit akzeptiert (BH, AA) — low, Guard wäre neue Verzweigung; H-7 garantiert die DB → abgelehnt.
- Alte KV-Einträge (`rev2:` …) und `ws_parts.state` bleiben auf Disk (BH) — false als Defekt: kein Leser mehr; Non-goal, TTL räumt KV.
- Quelltext-Test der `kv.set`-Schlüssel fragil (BH) — low → abgelehnt (Absicherung, nicht Verhalten).
- Reviewer-Diff ohne Spine/Spec (BH) — false: Spine-Zeilen H-2/H-3 geändert, nur nicht im Code-Diff.
- Metadaten-Webhook leert Where-Used-Fehltreffer nicht (BH) — low, vorbestehend (KV-Variante gleich), 1 h → abgelehnt; Doku nennt jetzt die 1 h.
- `elementType` fehlt (-1) gilt als „Teil“ (ECH) — low, wie alte Sync-Regel → abgelehnt.
- Revision `None` → Zeichnungs-Schlüssel `"None"` (ECH) — false: `_model_revision` liefert immer eine Revision, vorbestehend.
- Fehltreffer mit Zeitstempel in der Zukunft (ECH) — low, Uhrsprung → abgelehnt.
- `released_parts` zählt nun Alt-Zeilen mit nur untypisiertem Zeiger → mehr `link_drawings`-Kandidaten (ECH) — low/gewollt: das sind echte freigegebene Teile ohne Link; Calls begrenzt durch `maxParts` des Admins.
- Abschluss: 238 Tests + 544 Upstream grün.
