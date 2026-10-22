---
title: 'Voller Revisions-Sync über /admin/sync?full=1'
type: 'feature'
created: '2026-10-22'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-scoped-workflow-invalidation/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Nach dem Löschen durch ein Workflow-Event am 2026-10-21 hat die Produktion nur noch 1 von 429 Latest-Zeigern. Der inkrementelle `/admin/sync` holt nur Revisionen, die nach dem Cursor `rev_sync_after` liegen, und füllt die Zeiger deshalb nie wieder auf.

**Approach:** `?full=1` startet die Aufzählung ab 2000-01-01, unabhängig vom Cursor (SPEC CAP-5). Kosten: ⌈N/100⌉ Calls, heute ca. 5. Eric hat das am 2026-10-22 freigegeben.

</frozen-after-approval>

## Implementation Notes

- `extra_tools.py` `sync_revisions(full=...)`: Start ab 2000-01-01, `newest` startet beim alten Cursor (läuft nie rückwärts); Ergebnis meldet `full`.
- `app.py` `admin_sync`: `full` nur `""|0|1`, sonst 400 vor dem Cooldown; Audit-Feld `full`.
- Tests: `test_full_sync_refills_latest_pointers`, `test_full_sync_rejects_bad_flag` (tests/test_e2e.py); README, DOKUMENTATION, AGENTS.md ergänzt.

## Review Triage Log

- SPEC/Docstring versprachen aktualisiertes `isObsolete` — medium, patch: Behauptung entfernt (latestOnly liefert evtl. nicht alle Zeilen).
- Zeichnungslinks nach Full-Sync undokumentiert — medium, patch: SPEC, README, DOKUMENTATION.
- Ergebnis zeigt Modus nicht — low, patch: `"full"` im Ergebnis.
- Test-Lücken (`full=0`, Cooldown nach 400, Cooldown-Key hart codiert, timeout) — low, patch.
- Doku nennt Prüffeld nicht — low, patch: `table.latest_pointers` vs. `table.revisions`.
- Eigener, längerer Cooldown für Full-Runs — low, abgelehnt: manuelle Admin-Aktion, Budget-Caps greifen; Doku sagt „nicht einplanen“.
- Kein Resume nach unvollständigem Full-Run — low, abgelehnt: heute ~5 von max. 50 Seiten.
- Feste Call-Zahl „heute ~5“ veraltet — low, abgelehnt: als Richtwert markiert, Formel steht daneben.
- Artefakt war Stub — erledigt mit diesem Abschluss.

