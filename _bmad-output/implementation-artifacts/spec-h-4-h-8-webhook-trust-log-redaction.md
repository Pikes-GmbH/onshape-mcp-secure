---
title: 'H-4 + H-8: Webhooks erhöhen kein Vertrauen, Logs zeigen keine Geheimpfade'
type: 'bugfix'
created: '2026-10-15'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-webhook-trust-log-redaction/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `webhook.ping`/`webhook.register` an den Geheimpfad setzen `webhook_verified_at` und verlängern so die 30-Tage-TTL; Events ohne `webhookId` werden auch nach der Registrierung verarbeitet; `onshape.revision.created` lässt `ws_docs` sauber, sodass `ws_parts` Status/Revision bis 30 Tage veralten (H-4, AD-7). `auth_failed` loggt den vollen Pfad inkl. Webhook-Geheimnis bzw. Link-Token (H-8, AD-10, AD-13).

**Approach:** `webhooks.py`: Ping/Register schreiben nur noch `webhook_last_event_at`; bei gespeicherter Webhook-ID werden Events mit fehlender/unbekannter `webhookId` ignoriert (200); Release-Events mit gültiger `documentId` rufen zusätzlich `mark_doc_dirty(did)`, ohne gültige ID nichts. `auth.py`: Pfade unter `/onshape/webhook/`, `/img/`, `/dl/` vor dem Loggen auf Präfix + 6 Zeichen kürzen. Tests je Verhalten; H-4/H-8 in der Spine als erledigt markieren. Kein Commit/Deploy ohne Erics OK.

</frozen-after-approval>

## Implementation Notes

- `webhooks.py`: Ping/Register schreiben nur `webhook_last_event_at`; `webhookId` muss String und bekannt sein, sobald eine ID gespeichert ist (vorher 500 bei `webhookId` als Liste); `_document_id` prüft 24 Hex; Release-Event markiert nur gültiges Dokument dirty; Erfolgs-Log escaped und zeigt `doc=yes/no` (prüft in Produktion die Annahme, dass Release-Events `documentId` tragen).
- Metadaten-Event: gültige ID → dieses Dokument; Feld fehlt → alle (wie bisher); Feld vorhanden, aber ungültig → nichts (vorher: alle bei Länge ≠ 24).
- `auth.py`: `redact_path` kürzt `/onshape/webhook/`, `/img/`, `/dl/` auf Präfix + 6 Zeichen + `...`; `auth_failed` nutzt es. Uvicorn hat kein Access-Log (`log_level="warning"`); `image_served`/`download_served` loggten schon Präfixe.
- `webhook_active()` direkt nach Registrierung unverändert, weil `/admin/webhook` `webhook_verified_at` selbst setzt.
- Tests: e2e Ping/Register ohne Vertrauensgewinn, unbekannte/fehlende/Listen-ID ignoriert, Annahme vor Registrierung, Release-Event dirty (gültig/fehlt/ungültig), Metadaten ungültig/fehlend, Metadaten-ID bekannt, Webhook-Log escaped, `auth_failed` ohne Geheimnis; Unit `redact_path`. Gegenprobe: neue Tests schlagen gegen alten Code fehl. 173 grün.
- Spine: H-4, H-8 als erledigt markiert. Kein Commit (Erics OK ausstehend).

## Review Triage Log

- Metadaten-Event mit ungültiger ID markiert alle Docs dirty — medium, durch Änderung verursacht → patch + Test.
- Erfolgs-Log nicht escaped (Log-Forging) — low, vorbestehend, Fix trivial → patch + Test.
- Gate öffnet nach Verschwinden des Webhooks wieder — medium, Fix in `app.py` → defer (deferred-work.md).
- Annahme `documentId` in Release-Events ungeprüft — low → patch: Log-Feld `doc=yes/no`.
- Test für Metadaten-Webhook-ID fehlt — low → patch.
- Test „genau dieses Dokument“ (Fake hat nur ein Dokument) — low, Fake-Umbau nötig → abgelehnt; `mark_doc_dirty(did)` ist per WHERE auf eine ID begrenzt.
- `redact_path` case-sensitiv — false: Starlette-Routen sind case-sensitiv, `/IMG/…` ist kein Capability-Pfad.
- Präfixliste doppelt zu öffentlichen Routen — low, selten → abgelehnt.
- Spine-Zeilen bleiben in der Tabelle — false: Markierung „Erledigt“ war so verlangt.
- Spec unfertig — false: zum Review-Zeitpunkt in Arbeit, jetzt finalisiert.
- Doku zu Ping/`lastEventAt` — false: Doku beschreibt Ping-Semantik nicht.
- Docstring ungenau — low → patch.

### bmad-code-review (4 Layer: Blind Hunter, Edge Case Hunter, Verification Gap, Acceptance Auditor)

- `test_webhook_log_is_escaped` konnte nicht fehlschlagen (Level WARNING) — medium → patch: `log_level="INFO"` + Variante mit unbekannter ID (WARNING-Zeile).
- Metadaten-Testfälle unterschieden alt/neu nicht; `documentId: null` markierte nichts statt alle — medium → patch: fehlt/leer → alle, ungültig → keins; Fälle `"abc"`, `42`, `None`, `""`.
- `onshape.workflow.transition` markiert `ws_docs` nicht dirty (AD-7 „Release-Event“) — medium → patch + Test; SPEC CAP-3 erweitert.
- Access-Log nur zufällig aus (`log_level`) — low, Ein-Zeilen-Fix → patch `access_log=False`.
- `_document_id` dreifach berechnet, Log könnte abweichen — low → patch.
- Docstring „once“ untertrieben — low → patch.
- CAP-1 „leer“ ungetestet — low → patch (Ping vor Registrierung lässt `webhook_verified_at` leer).
- Spine-Zeilen nannten alte Abweichung als aktuell; H-4 nur teilweise belegt — low → patch: „war: …“, offene Punkte genannt.
- Gate öffnet nach Verschwinden des Webhooks — medium, bereits deferred.
- Kein Rate-Limit auf Webhook-Route — medium, vorbestehend → defer.
- 6 Zeichen Webhook-Geheimnis im Log vs. AD-7 „nie geloggt“ — low: Geheimnis ≥ 32 Zeichen, AD-10/AD-13 erlauben Präfix, Spec verlangt 6 → abgelehnt.
- `//onshape/webhook/…` bzw. Großschreibung nicht redigiert — low, kein gültiger Routenpfad, Fix wäre Zusatz-Guard → abgelehnt.
- Großgeschriebene Hex-IDs ignoriert — false: Onshape-IDs sind klein geschrieben.
- Ignorierte Events ohne Zähler in `/admin/webhook` — low: WARNING im Log sichtbar → abgelehnt.
- Einige neue Tests bestehen auch mit altem Code — false als Defekt: Regressionsschutz; Kerntests schlagen gegen alten Code fehl.
- Doku für Betreiber (Ping, gekürzte Pfade) — low, Doku beschreibt Ping-Semantik nicht → abgelehnt.
- Redaction-Tests für Query/`%2F`/kurze Segmente — false: `scope["path"]` enthält keine Query; kurze Segmente werden ebenfalls gekürzt (Unit-Fall `/img/`).
- Abschluss: 180 Tests grün.
