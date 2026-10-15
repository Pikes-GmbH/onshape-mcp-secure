---
id: SPEC-tool-registration-read-scope
companions:
  - ../../planning-artifacts/architecture/architecture-onshape-mcp-secure-2026-10-15/ARCHITECTURE-SPINE.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# H-9 + H-10: Read-Scope über Klasse `index`, konsistente Tool-Registrierung

## Why

Architektur-Backlog H-9 (AD-14) und H-10 (AD-2, AD-11) plus Deferred-Punkt aus H-1. Bei gesetztem Read-Scope (`READ_ALLOWED_DOCUMENT_IDS`) liefern `find_drawing_for_part` und die Suche nach unveröffentlichten Teilen (`_find_unreleased`) Daten aus Dokumenten ohne `check_read`; dokumentübergreifende Tools werden über eine Namensliste statt über eine Klasse gesperrt. Die vier Registrierungslisten eigener Tools sind ungeprüft, doppelte Namen überschreiben still. Tippfehler in `DISABLED_TOOLS` lassen ein Tool unbemerkt offen.

## Capabilities

- **CAP-1**
  - **intent:** Bei gesetztem Read-Scope geben eigene Tools nur Dokumente zurück oder nennen sie, die `check_read` bestehen: Zeichnungen aus `find_drawing_for_part`, Auswahl-Listen von `export_released_drawing`, Treffer und „Searched“-Hinweis der Suche nach unveröffentlichten Teilen.
  - **success:** Test mit Read-Scope: Zeichnung/Treffer in nicht erlaubtem Dokument fehlt im Ergebnis; Dokument außerhalb des Scopes wird nicht indexiert; explizites `documentId` außerhalb des Scopes → `Denied`.
- **CAP-2**
  - **intent:** Tool-Klasse `index` für dokumentübergreifende Listen-Tools; `is_tool_enabled` sperrt die Klasse bei gesetztem Read-Scope statt einer Namensliste.
  - **success:** `index` = `show_mapping_table`, `search_part_index`, `export_mapping_table`, `list_documents`, `search_documents`; mit Read-Scope ist keines davon veröffentlicht, ohne Read-Scope alle; keine Namensliste mehr in `is_tool_enabled`.
- **CAP-3**
  - **intent:** Ein Test belegt, dass `extra_tools.TOOLS`, `policy.TOOL_CLASSES`, `policy.EXTRA_TOOL_NAMES` und die Handler-Schlüssel von `ExtraTools` übereinstimmen.
  - **success:** Fehlt ein eigenes Tool in einer Liste, schlägt der Test fehl; Namen in `EXTRA_TOOL_NAMES` ohne Schema in `TOOLS` sind ersetzte Upstream-Tools (`get_parts`).
- **CAP-4**
  - **intent:** Doppelte veröffentlichte Tool-Namen brechen den Start mit klarer Fehlermeldung ab.
  - **success:** Doppelter Name in `extra_tools.TOOLS` (oder Kollision mit Upstream/`get_server_policy`) → `create_app` bzw. Lifespan-Start wirft einen Fehler, der den Namen nennt.
- **CAP-5**
  - **intent:** Unbekannte Namen in `DISABLED_TOOLS` beenden den Start.
  - **success:** `create_app` mit unbekanntem Namen → `ConfigError`, der den Namen nennt; bekannte Namen starten wie bisher.

## Constraints

- Produktion (fly.toml: kein Read-Scope, kein `DISABLED_TOOLS`) veröffentlicht exakt dieselben 26 Tools; Snapshot-Test gegen die echte `fly.toml`.
- Ohne Read-Scope keine zusätzlichen Onshape-Calls (`check_read` kehrt sofort zurück).
- Nicht erlaubte Dokumente werden still aus Ergebnissen entfernt (kein Hinweis auf ihre Existenz); explizit angefragtes `documentId` außerhalb des Scopes bleibt `Denied`.
- `config.py` importiert `policy` nicht; Namensprüfung in `create_app`.
- Kein Edit in `src/onshape_mcp/`; `extra_tools.py` wird nicht aufgeteilt (AD-11, späterer Schritt); Call-Scope/Audit (Paket 3) und Mapping-Eigentum (Paket 4) bleiben unberührt.

## Non-goals

- Keine neue Read-Scope-Funktion für Admin-Endpunkte (`/admin/sync`, `link_drawings`): nur Admin-Token.
- Kein Aufteilen von `extra_tools.py` in `tools/*`.
- Kein Deploy, kein Push, kein Commit ohne Erics OK.

## Success signal

- `python -m pytest -q tests` grün inkl. neuer Tests für CAP-1..CAP-5 und Produktions-Snapshot; H-9/H-10 in der Spine als erledigt markiert, Deferred-Eintrag `DISABLED_TOOLS` entfernt.

## Assumptions

- `index` wird außerhalb von `is_tool_enabled` wie `read` behandelt (`check_read` auf `documentId`-Argument, HTTP-Guard nur GET).
