---
id: SPEC-call-scope-data-dir
companions:
  - ../../planning-artifacts/architecture/architecture-onshape-mcp-secure-2026-10-15/ARCHITECTURE-SPINE.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# H-5 + H-6 + H-7: Ein Call-Scope für jeden Onshape-Zugriff, einheitliches Audit, Start nur mit beschreibbarem `/data`

## Why

Architektur-Backlog H-5, H-6 (AD-3, AD-8) und H-7 (AD-8). `fetch_document_name` umgeht über `raw_get` den HTTP-Guard; nur get/post/delete des Upstream-Clients sind gewrappt, eine neue Upstream-Methode liefe ungezählt an Onshape. Admin-Endpunkte loggen Freitext statt der JSON-`AUDIT`-Zeile, Kosten erscheinen mal als Requests, mal als abgerechnete Calls. Der `/admin/sync`-Cooldown lebt nur im Prozess und fällt bei jedem Auto-Stop weg. Ohne beschreibbares `/data` startet der Server trotzdem und schaltet Mapping und Caches still ab – Budgetzähler und Mapping gingen unbemerkt verloren.

## Capabilities

- **CAP-1**
  - **intent:** Jeder Onshape-Request (alle HTTP-Verben des Upstream-Clients, `get_bytes`, `fetch_document_name`) läuft durch `check_onshape_request`, reserviert vor dem Senden eine Budget-Einheit und erstattet sie bei 4xx/5xx/Transportfehler.
  - **success:** Test: Dokumentname-Lookup erscheint im Budget und in `onshapeCalls`; Fehlerantwort wird erstattet; Request ohne Call-Scope → `PolicyDenied`, kein Request an Onshape.
- **CAP-2**
  - **intent:** Der Start bricht ab, wenn der Upstream-Client eine öffentliche Coroutine-Methode hat, die weder gewrappt noch als Nicht-Request (`close`) bekannt ist.
  - **success:** Test auf der echten Klasse grün; Test mit hinzugefügter Methode (z. B. `put`/`get_raw`) → `create_app` wirft Fehler mit dem Namen.
- **CAP-3**
  - **intent:** Tools, Admin-Endpunkte (`/admin/webhook`, `/admin/sync`) und Hintergrundjobs (wöchentlicher Webhook-Check) öffnen denselben Call-Scope (Akteur, Aktion, Klasse, Dokument) und schreiben genau eine JSON-`AUDIT`-Zeile über `_audit` (event, decision, `onshapeCalls`, `attempts`, ms).
  - **success:** Tests parsen die `AUDIT`-Zeilen von Admin-Webhook, Admin-Sync (inkl. Cooldown-Ablehnung) und Webhook-Check als JSON; kein Freitext-`AUDIT` mehr im Code.
- **CAP-4**
  - **intent:** Kosten werden überall als abgerechnete Calls gemeldet: Verbrauchszeile, `AUDIT` `onshapeCalls`, Admin-Antworten `onshapeCalls`.
  - **success:** Test: fehlgeschlagener Request zählt in `attempts`, nicht in `onshapeCalls`; `/admin/webhook` liefert `onshapeCalls`.
- **CAP-5**
  - **intent:** Der `/admin/sync`-Cooldown (600 s) liegt in `mappings.db` `meta` und überlebt einen Neustart.
  - **success:** Test: Sync, neue App auf demselben Datenverzeichnis, zweiter Sync → 429 ohne Onshape-Request.
- **CAP-6**
  - **intent:** `create_app` verweigert den Start, wenn das Datenverzeichnis (Verzeichnis von `USAGE_FILE`) nicht beschreibbar ist oder Mapping-DB, KV-/Datei-Cache oder aktivierte Bild-/Link-Speicher nicht öffnen.
  - **success:** Tests: nicht beschreibbares Verzeichnis bzw. kaputte Mapping-DB → `DataDirError` (Unterklasse von `ConfigError`, `main()` beendet mit Exit 2); `tmp_path`-Setups starten wie bisher.

## Constraints

- Produktion sonst unverändert: dieselben Tools, Limits, Guard-Entscheidungen und Onshape-Call-Anzahlen pro Tool.
- Request außerhalb eines Call-Scopes wird abgelehnt (fail closed), nicht ungezählt gesendet.
- Ein Job innerhalb eines Tool-Calls schreibt eine eigene `AUDIT`-Zeile; seine Calls zählen zusätzlich in der Verbrauchszeile des Tools.
- Beschreibbarkeit per Probe-Datei (anlegen + löschen), nicht `os.access`.
- Kein Edit in `src/onshape_mcp/`; Tests nie gegen echtes Onshape.
- Paket 4 (Mapping-Eigentum, KV-Fallbacks) und 5 (API-Version, Budget-Warnung) bleiben unberührt.

## Non-goals

- Kein neues Audit-Ziel (Datei, externer Dienst); nur Log-Zeilen.
- Keine Änderung an Budget-Limits oder `fly.toml`.
- Kein Deploy, kein Push, kein Commit ohne Erics OK.

## Success signal

- `python -m pytest -q tests` und Upstream-Tests grün inkl. neuer Tests für CAP-1..CAP-6; `grep "AUDIT "` findet nur `_audit`; H-5/H-6/H-7 in der Spine als erledigt markiert.

## Assumptions

- Der Cooldown startet auch bei fehlgeschlagenem Sync (wie heute), damit Wiederholungen kein Budget verbrennen.
- Audit-Feld `onshape_requests` wird zu `onshapeCalls` (billed) + `attempts`; README/DOKUMENTATION werden angepasst.
