---
id: SPEC-scoped-workflow-invalidation
companions:
  - ../../planning-artifacts/architecture/architecture-onshape-mcp-secure-2026-10-15/ARCHITECTURE-SPINE.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Gezielte Invalidierung bei `onshape.workflow.transition`

## Why

`webhooks.py` ruft bei jedem `onshape.workflow.transition` `mapping.invalidate_all()` auf: alle `latest`-Zeiger, Teil→Zeichnung-Links und Where-Used-Misses sind danach weg. Am 2026-10-22 zeigte die Produktion 429 Revisionen, aber nur noch 1 Latest-Zeiger und 1 Zeichnungslink, nachdem am 2026-10-21 um 15:27 UTC ein Workflow-Event kam. Seitdem kostet jeder Teile-Lookup einen Onshape-Call. Jeder Freigabe- oder Obsoleszenz-Schritt löst so ein Event aus, das Budget liegt bei 2.500 Calls/Jahr.

Payload laut Onshape-OpenAPI (`BTWorkflowMessageBody`, abgerufen am 2026-10-22): `objectId`, `objectType` (`RELEASE|TASK|ASSIGNMENT|OBSOLETION`), `transitionName`, `workflowId`, `data`, `webhookId`. Es gibt **kein** `documentId` und **kein** `partNumber`. Revisionen speichern lokal ihre `releaseId`, deshalb lässt sich eine bekannte `objectId` ohne API-Call auf Teilenummern abbilden.

## Capabilities

- **CAP-1**
  - **intent:** Trifft die `objectId` (24 Hex-Zeichen) eines Workflow-Events die `releaseId` gespeicherter Revisionen, werden nur deren Teilenummern invalidiert (`latest`, `drawings`, `whereused_misses`) und ihre Dokumente im Working-State-Index dirty markiert. Ist darunter eine Zeichnung, fallen alle Zeichnungslinks weg, wie bei `onshape.revision.created`.
  - **success:** Unit-/E2E-Test: Event mit bekannter Release-ID entfernt die Zeiger der betroffenen Teile; Zeiger anderer Teile bleiben, und ihr Lookup kostet 0 Onshape-Calls.
- **CAP-2**
  - **intent:** Events, die keine Freigabedaten betreffen oder schon anders abgedeckt sind, invalidieren nichts: `objectType` `TASK`/`ASSIGNMENT` sowie `RELEASE` mit unbekannter ID. Neue Freigaben kommen per `revision.created` pro Revision, verlorene Events fängt die `droppedEventCount`-Prüfung.
  - **success:** Tests: Für jeden dieser Fälle bleiben alle `latest`-Zeiger und Zeichnungslinks erhalten.
- **CAP-3**
  - **intent:** Ein volles `invalidate_all` gibt es nur noch, wenn der Payload nichts Verwertbares identifiziert. Das betrifft drei Fälle: (1) `OBSOLETION`, das nie trifft, weil gespeicherte Revisionen älter sind als das Obsoleszenz-Paket; (2) einen fehlenden oder unbekannten `objectType` ohne lokalen Treffer; (3) `RELEASE` mit fehlender oder fehlerhafter `objectId`. `TASK`/`ASSIGNMENT` invalidieren nie.
  - **success:** Tests: Jeder dieser Payloads leert die Zeiger wie bisher.
- **CAP-4**
  - **intent:** Die Log-Zeile des Webhooks zeigt `objectType` und `transitionName` (JSON-escaped, gekürzt) sowie `invalidated=parts:N|none|all`. So werden die echten Werte und der gewählte Zweig im Produktionslog sichtbar. `docs/DOKUMENTATION.md` beschreibt die Regel.
  - **success:** Test: Die Felder erscheinen escaped im Log, und eingeschleuste Zeilenumbrüche erzeugen keine neue Logzeile.

## Constraints

- AD-7: Webhooks löschen oder markieren nur. Sie schreiben keine Fachdaten, setzen keine Vertrauensmarker und rufen Onshape nie auf.
- Events von fremden Webhooks werden weiter ignoriert. `revision.created`, `metadata` und das Markieren eines vorhandenen `documentId` ändern sich nicht.
- Kein Edit in `src/onshape_mcp/`. Tests nie gegen echtes Onshape. `fly.toml`-Limits bleiben unverändert.

## Non-goals

- Kein Full-Sync in diesem Paket (Vorschlag siehe Open questions).
- Kein Pauschal-Löschen aller Zeichnungslinks bei Zeichnungsfreigabe in `on_revision_created` umbauen.
- Veraltetes `isObsolete` in gecachten Revisionszeilen wird hier nicht korrigiert.
- Kein Deploy, kein Push, kein Commit ohne Erics OK.

## Success signal

- `python -m pytest -q tests` grün, inklusive neuer Tests für CAP-1..CAP-4. Ein `workflow.transition` vom Typ `TASK` oder `RELEASE` mit unbekannter ID lässt `mapping.summary()["latest_pointers"]` unverändert.

## Assumptions

- Ein bereits freigegebenes Release-Paket ändert über weitere Transitions keine freigegebenen Daten. Unbekannte `RELEASE`-IDs brauchen deshalb keine Invalidierung.
- Die Werte von `transitionName` sind nicht dokumentiert. Deshalb trifft der Code damit noch keine Entscheidung, sondern loggt sie nur (CAP-4).

## Open questions

- **Vorschlag an Eric:** `POST /admin/sync?full=1` ignoriert den Cursor `rev_sync_after` und zählt alle aktuellen Revisionen neu auf (`latestOnly`, 100 pro Seite). Das füllt die `latest`-Zeiger wieder und aktualisiert `isObsolete`. Kosten: ⌈N/100⌉ Calls, heute ca. 5 bei 429 Revisionen; der Cap liegt bei 50 Seiten. Optional setzt `invalidate_all` den Cursor zurück, sodass der nächste Sync automatisch voll läuft. Zeichnungslinks brauchen weiter `link_drawings` (1 Call pro Teil). Umsetzung erst nach OK.
