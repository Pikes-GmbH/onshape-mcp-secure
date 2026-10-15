---
id: SPEC-webhook-version-budget-warning-backups
companions:
  - ../../planning-artifacts/architecture/architecture-onshape-mcp-secure-2026-10-15/ARCHITECTURE-SPINE.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# H-11 + H-12 + H-13: Webhook-API-Version, Budget-Warnung, Snapshots

## Why

Letztes Paket des Hardening-Backlogs der Spine. H-11: eigener Code ruft `/api/webhooks` unversioniert auf (Registrierung, wöchentliche Prüfung, Guard-Regex) – verstößt gegen die Konvention „Onshape-API-Pfade mit expliziter Version“. H-12: Budget 2.500 Calls/Jahr firmenweit; niemand merkt heute, dass Monats- oder Jahresbudget fast aufgebraucht ist (Spine „Monitoring“). H-13: README empfiehlt, Volume-Snapshots abzuschalten; Eric hat entschieden, dass sie anbleiben, weil sie `mappings.db` und den Budgetzähler sichern.

## Capabilities

- **CAP-1**
  - **intent:** Registrierung und wöchentliche Prüfung des Webhooks nutzen `/api/v6/webhooks`; der HTTP-Guard lässt für Klasse `system` nur genau diese versionierten Pfade als modifizierende Requests durch.
  - **success:** Tests: Guard erlaubt `POST /api/v6/webhooks` und `POST`/`DELETE /api/v6/webhooks/<24 hex>` für `system`, lehnt unversionierte, andere Versionen, Unterpfade und andere Klassen ab; E2E-Registrierung und Prüfung gehen gegen `/api/v6/webhooks`.
- **CAP-2**
  - **intent:** Erreicht Monats- oder Jahresverbrauch ≥ 80 % seines Limits, trägt die `[Onshape-API: …]`-Zeile jedes Tool-Ergebnisses einen kurzen deutschen Warnhinweis, den der Agent an den Nutzer weitergibt; `get_server_policy` zeigt dieselbe Warnung.
  - **success:** Tests bei 79 %, 80 % und 100 % (Monat und Jahr): ohne/mit Warnung in Verbrauchszeile und `get_server_policy`; unbegrenzte Limits warnen nie.
- **CAP-3**
  - **intent:** README und `docs/DOKUMENTATION.md` sagen, dass Volume-Snapshots anbleiben (sichern Mapping-DB und Budgetzähler; Dateien darin höchstens 24 h alt), ohne Abschalt-Empfehlung.
  - **success:** `grep scheduled-snapshots=false README.md docs/` leer; beide Dokumente nennen den Grund.

## Constraints

- Kein neuer Benachrichtigungskanal (AD-14), keine zusätzlichen Onshape-Calls, Limits in `fly.toml` unverändert.
- Bereits registrierte Webhook-IDs bleiben gültig (IDs sind versionsunabhängig); keine Neuregistrierung nötig.
- Schwelle ganzzahlig (`used*100 >= 80*limit`); Tages- und Wochenlimit warnen nicht (die blocken ohnehin früh).
- Kein Edit in `src/onshape_mcp/`; Tests nie gegen echtes Onshape.

## Non-goals

- Kein Ping-/Update-Endpunkt für Webhooks, keine Migration bestehender Webhooks.
- Keine Warnung per Mail/ClickUp/Log-Alarm; keine Änderung an Limits oder Budget-Logik.
- Kein Deploy, kein Push, kein Commit ohne Erics OK.

## Success signal

- `python -m pytest -q tests` und Upstream-Tests grün inkl. neuer Tests für CAP-1..CAP-3; `grep '"/api/webhooks' src/onshape_secure` leer; H-11..H-13 in der Spine erledigt und der Backlog als abgeschlossen markiert.
