---
id: SPEC-mapping-ownership
companions:
  - ../../planning-artifacts/architecture/architecture-onshape-mcp-secure-2026-10-15/ARCHITECTURE-SPINE.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# H-2 + H-3: Mapping-DB ist alleiniger Eigentümer der Teile-Daten

## Why

Architektur-Backlog H-2 (AD-6, AD-12) und H-3 (AD-6). `_revision`, `_drawings_where_used` und `_locate_by_where_used` legen Teile-Daten ohne Mapping-DB im KV-Cache ab (`rev2:`, `whereused5:`, `wu_pn_miss:`) – zwei Wahrheiten, KV-Schlüssel pro Teilenummer. Seit H-7 startet der Server nicht ohne `/data`, der Modus „ohne Mapping-DB“ ist toter Code. `put_revision` (Lookup) und `put_synced_revision` (Sync) lösen den untypisierten Latest-Zeiger `latest[pn,'']` unterschiedlich auf: Sync bevorzugt Teil/Baugruppe vor gleichnamiger Zeichnung, der Lookup speichert, was Onshape liefert. `ws_parts` liefert zusätzlich Freigabestatus/-revision aus dem Arbeitsstand.

## Capabilities

- **CAP-1**
  - **intent:** Teile-Lookups, Teil→Zeichnung-Links und „Where Used per Teilenummer“-Fehltreffer lesen und schreiben nur `mappings.db`; die KV-Schlüssel `rev2:`, `whereused5:`, `wu_pn_miss:` werden weder gelesen noch geschrieben.
  - **success:** Tests: nach Lookup, Zeichnungssuche und Where-Used-Fehltreffer enthält `cache.json` keinen dieser Schlüssel; der Fehltreffer liegt in `mappings.db` und spart innerhalb 1 h den zweiten Call.
- **CAP-2**
  - **intent:** `ExtraTools` verlangt die Mapping-DB; alle Zweige für „Mapping fehlt“ entfallen.
  - **success:** `grep "mapping is None"` in `extra_tools.py` leer; `ExtraTools` ohne `mapping` wirft `TypeError`.
- **CAP-3**
  - **intent:** `mapping.py` hat pro Tabelle/Zeilenart genau eine Schreibfunktion (Revisionszeile, Latest-Zeiger inkl. untypisierter Auflösung, Not-found-Marker, Where-Used-Fehltreffer); Lookup und Sync nutzen dieselben, die Auflösungsregel steht einmal.
  - **success:** Test: Sync und On-Demand-Lookup derselben Revisionen (Teil + gleichnamige Zeichnung, beide Reihenfolgen) ergeben identische `revisions`- und `latest`-Zeilen; Test für veralteten Teil-Zeiger.
- **CAP-4**
  - **intent:** Freigabestatus und freigegebene Revision kommen nur aus `revisions`/`latest`; die Indexierung schreibt `ws_parts.state`/`revision` nicht mehr, Tools und Export geben sie nicht aus.
  - **success:** Tests: `find_part_by_number` (nicht freigegeben), `search_part_index` und Export-Zeilen aus dem Arbeitsstand enthalten keinen Status/keine Revision aus `ws_parts`.

## Constraints

- Keine zusätzlichen Onshape-Calls pro Lookup (Budget 2.500/Jahr).
- Bestehende `mappings.db`/`cache.json` in Produktion funktionieren ohne Neuaufbau; Schema nur additiv (`CREATE TABLE IF NOT EXISTS`), kein `INDEX_VERSION`-Sprung (würde alle Dokumente neu indexieren).
- Untypisierte Regel: Teil/Baugruppe (jeder Nicht-Zeichnungs-Typ) wird immer untypisierter Zeiger; eine Zeichnung nur, wenn der aktuelle fehlt, Not-found ist, selbst Zeichnung ist oder älter als die Latest-TTL (sonst Refetch-Schleife bei veraltetem Teil-Zeiger).
- KV hält nur Metadaten unter unveränderlichen IDs (Version, Release-Paket, Company) und laufende Jobs; Company-ID gehört `meta`, Alt-Wert aus KV wird einmal übernommen.
- Kein Edit in `src/onshape_mcp/`; `extra_tools.py` wird nicht aufgeteilt; Tests nie gegen echtes Onshape.

## Non-goals

- Keine Aufteilung von `extra_tools.py` (AD-11), kein Paket 5 (API-Version Webhooks, Budget-Warnung, README-Snapshots).
- Kein Löschen alter KV-Einträge oder `ws_parts`-Spalten.
- Kein Deploy, kein Push, kein Commit ohne Erics OK.

## Success signal

- `python -m pytest -q tests` und Upstream-Tests grün inkl. neuer Tests für CAP-1..CAP-4; `grep -E "rev2:|whereused5|wu_pn_miss|mapping is None" src/onshape_secure` leer; H-2/H-3 in der Spine erledigt.

## Assumptions

- Verwaiste KV-Einträge laufen über ihre TTL und die KV-Bereinigung ab.
- Ein Arbeitsstand-Treffer ist ohnehin als „NOT released“ markiert; `get_parts` (Live-Call) behält sein `state`.
