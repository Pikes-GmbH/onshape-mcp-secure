---
title: 'Gezielte Invalidierung bei onshape.workflow.transition'
type: 'bugfix'
created: '2026-10-22'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-scoped-workflow-invalidation/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Jedes `onshape.workflow.transition` löst `mapping.invalidate_all()` aus. In Produktion blieben danach von 429 Revisionen nur 1 Latest-Zeiger und 1 Zeichnungslink übrig, und jeder Lookup kostet wieder einen Onshape-Call. Laut OpenAPI trägt der Payload nur `objectId`, `objectType` (`RELEASE|TASK|ASSIGNMENT|OBSOLETION`) und `transitionName`, aber kein `documentId` und kein `partNumber`.

**Approach:** Die `objectId` wird lokal gegen die gespeicherte `releaseId` der Revisionen abgebildet. Bei einem Treffer werden nur diese Teilenummern und Dokumente invalidiert. `TASK`, `ASSIGNMENT` und unbekannte `RELEASE`-IDs invalidieren nichts. `invalidate_all` bleibt nur für `OBSOLETION` ohne Treffer sowie für fehlenden oder unbekannten `objectType` bzw. eine ungültige ID ohne Treffer. Das Log zeigt `objectType` und `transitionName` escaped (Details: SPEC CAP-1..4).

</frozen-after-approval>

## Implementation Notes

- `mapping.py`: `on_workflow_transition(object_id, object_type)` liefert die betroffenen Dokument-IDs zurück; `webhooks.py` markiert diese als dirty.
- Teilenummer-Invalidierung wiederverwendet aus `on_revision_created` (Hilfsfunktion `_invalidate_part`).
- Review-Patches: Rückgabe `(scope, docs)` für die Log-Spalte `invalidated=`; Docstring/Testkommentar zu Obsoleszenz korrigiert; E2E prüft Dirty-Markierung des Dokuments; `docs/DOKUMENTATION.md` ergänzt.

## Review Triage Log

- Obsoleszenz trifft nie (Docstring/Test irreführend) — medium, patch: Doku korrigiert, Verhalten (invalidate_all) ist spec-konform; Eingrenzung deferred.
- Spec vs. Code bei fehlerhafter objectId + TASK — low, patch: SPEC CAP-3 präzisiert (TASK/ASSIGNMENT invalidieren nie).
- Bekanntes Paket wird bei erneuter Transition invalidiert — low, abgelehnt: gezielt, billig, sicherheitshalber frisch.
- Log zeigt gewählten Zweig nicht — medium, patch: `invalidated=parts:N|none|all`.
- Dirty-Markierung im Handler ungetestet — medium, patch: E2E prüft `index_calls`.
- Ganze revisions-Tabelle wird je Event dekodiert — low, abgelehnt: ~430 Zeilen, seltene Events.
- Treffer auf alter Revision invalidiert aktuellen Zeiger — low, abgelehnt: selten, kostet höchstens 1 Call.
- Invalidierung nicht atomar — low, abgelehnt: bestehendes Muster (`invalidate_all`), Teilzustand ist nur „weniger Cache“.
- Produktions-Zeiger nicht wiederhergestellt — medium, defer (Full-Sync, braucht Erics OK).
- Doku fehlt — medium, patch: DOKUMENTATION.md; Spine bleibt unverändert (AD-7 gilt weiter).
- Log-Tests dünn / unrealistisches transitionName — low, teilweise patch (transitionName aus E2E-Payload entfernt).

### Review Findings (bmad-code-review, 2026-10-22)

- [x] [Review][Patch] TASK/ASSIGNMENT mit kollidierender releaseId invalidierte [src/onshape_secure/mapping.py] — Kurzschluss vor den Treffer gezogen
- [x] [Review][Patch] Fehlerhafte objectId (`"abc"`, `42`) und TASK mit bekannter ID nicht E2E getestet [tests/test_e2e.py]
- [x] [Review][Patch] Where-Used-Miss des getroffenen Teils nicht geprüft [tests/test_units.py]
- [x] [Review][Patch] Doku verschwieg Löschen aller Zeichnungslinks bei Zeichnung im Paket [docs/DOKUMENTATION.md]
- [x] [Review][Patch] `invalidated=-` bei Nicht-Workflow-Events undokumentiert [src/onshape_secure/webhooks.py]
- [x] [Review][Patch] Neue deferred-work-Einträge auf Englisch [deferred-work.md]

Rejected:
- Verlorene `revision.created` erst nach ≤ 7 Tagen erkannt — low, vorbestehender Wochen-Check, verlorene Events selten.
- Obsoleszenz lässt Working-State-Index veraltet — false: `ws_parts` führt keinen Freigabestatus (AD-6, H-3).
- `review_loop_iteration: 0` trotz Triage — false: Oneshot-Route ohne Loopback.
- Dirty-Prüfung indirekt / Multi-Dokument-Paket — low, Mengenlogik trivial.
- Kein Runbook für Produktions-Refill — bereits als deferred erfasst (Full-Sync).
- Success signal widerspricht CAP-1 — Spec-Fix, in SPEC.md über memlog nachgezogen.
- objectType-Injection im Log-Test — low, derselbe Escape-Pfad wie transitionName.
- Treffer über alte Revision — low, höchstens 1 Call.
- Zeichnung mit element_type -1 — low, Sync/Lookup liefern elementType immer.
- Korrupte info-JSON → 500 — false: Zeilen schreibt nur `json.dumps(dict)` dieses Moduls.
- objectType in anderer Schreibweise — low: OpenAPI-Enum ist groß; Log zeigt Abweichungen, Folge wäre das alte Verhalten.
- Treffer ohne documentId — low, beide Schreibpfade speichern documentId.
- Race Lookup vs. Invalidierung — low, vorbestehend wie bei `revision.created`.
