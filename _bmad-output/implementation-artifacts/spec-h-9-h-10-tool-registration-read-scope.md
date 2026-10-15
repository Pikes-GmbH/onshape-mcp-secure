---
title: 'H-9 + H-10: Read-Scope über Klasse index, konsistente Tool-Registrierung'
type: 'bugfix'
created: '2026-10-15'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-tool-registration-read-scope/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Bei gesetztem Read-Scope (`READ_ALLOWED_DOCUMENT_IDS`) liefern `find_drawing_for_part` (Zeichnungen aus fremden Dokumenten) und `_find_unreleased` (Index-Treffer, Indexierung, „Searched“-Hinweis) Daten ohne `check_read`; dokumentübergreifende Tools werden über eine Namensliste in `is_tool_enabled` gesperrt (H-9, AD-14). Die vier Registrierungslisten eigener Tools sind ungetestet, doppelte Namen überschreiben still (H-10, AD-2, AD-11). Unbekannte `DISABLED_TOOLS`-Namen werden still akzeptiert (Deferred aus H-1).

**Approach:** `policy.py`: Klasse `INDEX = "index"` für `show_mapping_table`, `search_part_index`, `export_mapping_table`, `list_documents`, `search_documents`; `is_tool_enabled` sperrt `INDEX` bei Read-Scope. `extra_tools.py`: Helfer `_readable(did)`; Zeichnungs- und Unreleased-Treffer filtern, Indexierung nur erlaubter Dokumente, explizites `documentId` prüft `check_read`; Handler-Tabelle als `HANDLERS`-Namen abfragbar. `app.py`: `create_app` prüft `DISABLED_TOOLS` gegen bekannte Namen (`ConfigError`) und doppelte eigene Namen; `_published_tools` bricht bei Duplikaten ab und läuft im Lifespan vor `yield`. Tests je Verhalten plus Snapshot der Produktions-Tools aus `fly.toml`. Spine H-9/H-10 erledigt, Deferred-Eintrag entfernt. Kein Commit ohne Erics OK.

</frozen-after-approval>

## Implementation Notes

- `policy.py`: `INDEX = "index"`; `list_documents`/`search_documents` (vorher `read`) und `show_mapping_table`/`search_part_index`/`export_mapping_table` (vorher `meta`) sind `index` – exakt die alte Namensliste. `is_tool_enabled`: `cls == INDEX and read_restricted`. Upstream-Listen nutzen nur GET, HTTP-Guard unverändert korrekt.
- `extra_tools.py`: `_readable()` (check_read → bool) und `_ws_hits()`; `find_drawing_for_part` und `_drawing_target` filtern Where-Used-Zeichnungen; `_find_unreleased` prüft explizites `documentId`, filtert Treffer, indexiert nur erlaubte Dokumente (vorher brach `_index_document` mit `Denied` ab); `_not_found` (jetzt async) nennt nur erlaubte Dokumentnamen. `handler()` nutzt neue `handlers()`-Tabelle (testbar). Ohne Read-Scope kehrt `check_read` sofort zurück: 0 Zusatz-Calls.
- `app.py`: `_check_tool_registry` in `create_app` (doppelte eigene Namen / Kollision mit `get_server_policy` → `RuntimeError`; unbekannte `DISABLED_TOOLS` → `ConfigError`, geprüft gegen `TOOL_CLASSES` ∪ `EXTRA_TOOL_NAMES`, die laut `test_every_upstream_tool_is_classified` alle Upstream-Namen enthalten). `_published_tools` prüft Upstream-Kollisionen und läuft im Lifespan vor `yield`. `main()` meldet `ConfigError` aus `create_app` als Konfig-Fehler (Exit 2, eine Zeile); andere Fehler mit Traceback. `_drawing_target` filtert auch das Release-Paket (Review).
- Tests: Unit – Produktions-Snapshot der 26 Tools aus echter `fly.toml`, `index` = alte Namensliste, Read-Scope blendet genau `index` aus, vier Registrierungslisten stimmen überein, Duplikat/Upstream-Kollision stoppt Start, unbekannte/bekannte `DISABLED_TOOLS`. E2E – Zeichnung aus fremdem Dokument fehlt (kein Export dorthin), innerhalb des Scopes unverändert, Unreleased-Treffer/Dokumentname aus fremdem Dokument fehlen, Screenshot ohne Render, explizites `documentId` → Denied, kein Indexieren fremder Dokumente. Gegenprobe: die Filter-Tests schlagen gegen alten Code fehl. Nach Review 196 + 544 Upstream grün.
- Doku: Spine H-9/H-10 erledigt, AD-14 Wortlaut („dokumentübergreifend listen“ inkl. Dokumentliste/-suche), README/DOKUMENTATION zu Read-Scope und `DISABLED_TOOLS`; Deferred-Eintrag entfernt.

## Review Triage Log

Oneshot-Layer Blind Hunter übersprungen, weil `bmad-code-review` (4 Layer: Blind Hunter, Edge Case Hunter, Verification Gap, Acceptance Auditor) direkt folgte. Alle Patches angewendet (headless, Auftrag aus Delegations-Session).

- `is_tool_enabled` nutzte weiter die Namensliste (alle 4 Layer) — high, durch diese Änderung verursacht (Ersetzung griff nicht: `\`+Zeilenumbruch im Python-Heredoc) → patch: `cls == INDEX and read_restricted`.
- Tests fingen das nicht (Set-Gleichheit mit alter Liste) — medium → patch: Test mit neuem `INDEX`-Tool per `monkeypatch.setitem`.
- `export_released_drawing`-Fallback Release-Paket: einzelne Zeichnung außerhalb des Scopes → `Denied` verrät Existenz — medium → patch: `_drawings_in_release` gefiltert + Test (PRT-000800).
- Export-Ergebnis im Scope-Test ungeprüft — medium → patch: kein Fehler, kein „Read access“.
- `todo`-Filter prüfte alle bekannten Dokumente vor dem Slice (Name-Lookups bei Read-Scope + `WRITE_MODE!=off`) — medium → patch: lazy Schleife bis `MAX_INDEX_DOCS_PER_LOOKUP`.
- Stale Dokument außerhalb des Scopes nicht getestet — medium → patch: `test_read_scope_skips_stale_documents_outside_scope`; Gegenprobe schlägt ohne Filter fehl.
- `test_read_scope_never_indexes_other_documents` unterschied Skip und Abbruch nicht — medium → patch: „STOP“ statt „Read access“.
- `main()` meldete jeden `create_app`-Fehler als Konfig-Fehler — low, Ein-Zeilen-Fix → patch: nur `ConfigError`.
- Kollision mit `get_server_policy` ungetestet — low → patch + Test.
- DOKUMENTATION.md ohne „Treffer aus anderen Dokumenten weg“ — low → patch.
- `_not_found` bis zu 10 Name-Lookups bei Read-Scope + Schreibmodus — low, nur in einer heute ungenutzten Konfiguration, 10 s gecacht, Fix bräuchte zweiten Prüfpfad → abgelehnt.
- `_readable(None)` lässt Zeichnungen ohne `documentId` durch — false: `parse_where_used`, `_released_drawing` und `_drawings_in_release` verlangen 24-Hex-IDs.
- `link_drawings` (Admin) ungefiltert — false: Admin-only (Non-goal); gespeicherte Links liefern `find_drawing_for_part` (filtert) und `index`-Tools (bei Scope gesperrt).
- `get_server_policy` in `DISABLED_TOOLS` stoppt jetzt den Start — false als Defekt: war wirkungslos (immer veröffentlicht); Produktion setzt `DISABLED_TOOLS` nicht.
- `DISABLED_TOOLS` gegen echte Upstream-Liste prüfen — false: `test_every_upstream_tool_is_classified` hält `TOOL_CLASSES` gleich Upstream ∪ eigene.
- Duplikatprüfung an zwei Stellen/Zeitpunkten — low, beide vor Annahme von Requests (Lifespan) → abgelehnt.
- Snapshot prüft `TOOL_CLASSES` statt `list_tools` — low: veröffentlicht = aktivierte Upstream-Tools + eigene + Policy-Tool, Klassifizierungstest deckt Upstream ab → abgelehnt.
- Mehrfach-Zeichnungs-Optionsliste ungetestet — low, Fake liefert nur eine Zeichnung, Umbau nötig → abgelehnt; Filter steht vor beiden Zweigen.
- Groß-/Leerzeichen-Varianten in `DISABLED_TOOLS` ungetestet — low: jede Abweichung ist unbekannt → `ConfigError` (parametrisierter Test deckt Tippfehler) → abgelehnt.
- Spec nicht im Diff — false: untracked Dateien, Reviewer bekamen den Pfad.
- Abschluss: 196 Tests + 544 Upstream grün.
