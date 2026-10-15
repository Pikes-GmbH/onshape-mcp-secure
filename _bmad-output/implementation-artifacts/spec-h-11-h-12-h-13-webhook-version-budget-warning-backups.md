---
title: 'H-11/H-12/H-13: Webhook-API-Version, Budget-Warnung, Snapshots'
type: 'chore'
created: '2026-10-15'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-webhook-version-budget-warning-backups/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Eigener Code ruft `/api/webhooks` ohne API-Version auf (Registrierung, Wochenprüfung, Guard). Niemand wird gewarnt, wenn Monats- oder Jahresbudget fast verbraucht ist. README empfiehlt, Volume-Snapshots abzuschalten, obwohl sie Mapping-DB und Budgetzähler sichern (Spine H-11, H-12, H-13).

**Approach:** Ein Pfad `/api/v6/webhooks` (Konstante in `policy.py`) für Aufrufe und Guard; Guard lässt für `system` nur die versionierten Pfade durch. Ab ≥ 80 % Monats- oder Jahreslimit hängt die `[Onshape-API: …]`-Zeile einen deutschen Warnhinweis an, `get_server_policy` zeigt ihn. README/DOKUMENTATION: Snapshots bleiben an, mit Grund. Tests für Guard, E2E-Pfade, 79/80/100 %. Kein Commit, kein Deploy ohne Erics OK.

</frozen-after-approval>

## Implementation Notes

- H-11: `policy.py` `WEBHOOKS_PATH = "/api/v6/webhooks"` (v6 wie Onshape-Doku und unsere `/api/v6/documents`-Aufrufe); `app.py` nutzt es für beide Registrierungen und die Wochenprüfung. Guard `system`: `POST` genau auf `WEBHOOKS_PATH`, `POST`/`DELETE` auf `WEBHOOKS_PATH/<24 hex>` (Regex mit `re.escape`); unversioniert, andere Versionen, Unterpfade, Großbuchstaben-IDs → `PolicyDenied`. E2E-Fake antwortet nur noch auf v6.
- H-12: `quota.py` `WARN_PERCENT = 80`, `budget_warning(snapshot)` (ganzzahlig `used*100 >= 80*limit`, nur Monat/Jahr, `"unlimited"` warnt nie). `_usage_line` hängt die Warnung als letztes `·`-Glied **innerhalb** der Klammern an (Zeile endet weiter mit `]`); `get_server_policy` hat `budget_warning` und `budget_warning_percent`; Server-Instructions: Warnung zitieren und unnötige Calls vermeiden. Gilt auch für `Denied`-Ergebnisse bei erschöpftem Budget.
- H-13: README-Abschalt-Hinweis ersetzt („Snapshots bleiben an“, Grund); DOKUMENTATION Grenzen + Große Dateien; Budget-Warnung in README/DOKUMENTATION beschrieben.
- Spine: H-1 (war nicht markiert, Commit 0746123), H-11..H-13 erledigt, Backlog als abgeschlossen markiert.
- Tests: 79/80/100 % Monat und Jahr (unit), Tag/Woche warnen nie (unit), E2E Monat 60/80/100 % + Denied, E2E Jahr 79/80 % (vorbelegtes `usage.json`), ohne Limits keine Warnung, Guard-Positiv-/Negativfälle. 263 Tests + 544 Upstream grün.
- Commit mit Erics OK; kein Push, kein Deploy.

## Review Triage Log

- Spine/Artefakt nicht abgeschlossen – real (low), war ohnehin nächster Schritt; erledigt.
- E2E deckt 100 %/Denied/unbegrenzt nicht ab – real (low); gepatcht (Tests ergänzt).
- Keine Warnung im AUDIT-Log/Admin-Status – real (low), außerhalb Spec (AD-14: Agent ist der Kanal); deferred.
- „verbatim“ vs. KI-Anweisung im Warntext – real (medium); gepatcht: Warntext nur Fakten, Anweisung steht in den Instructions.
- Zeile endete nicht mehr mit `]` – real (medium, Konvention „letzte Zeile ist die `[Onshape-API: …]`-Zeile“); gepatcht: Warnung innerhalb der Klammern.
- Restore aus Snapshots undokumentiert (WAL, Budget-Rücksprung) – real (low), Betriebsdoku außerhalb H-13; deferred.
- Guard erlaubt ungenutztes `POST`/`DELETE` auf Webhook-ID – low, vorbestehend; für Rotation per Neuregistrierung (Spine „Secrets“) absehbar nötig, nur Klasse `system`; abgelehnt.
- Webhook-IDs versionsunabhängig unbelegt – maybe-false (low): Onshape-IDs sind global, nur gegen echtes Onshape prüfbar; nach Deploy erste `verify_webhook`-AUDIT-Zeile prüfen (404 → Neuregistrierung). Abgelehnt, Hinweis an Eric.
- Guard-Negativlücken (DELETE mit READ/WRITE, Slash) – real (low), trivial; gepatcht.
- `budget_warning` bei kaputtem Snapshot – false: Snapshot kommt nur aus `OnshapeBudget.snapshot()` mit allen Schlüsseln, Limits int oder `"unlimited"`.


### Review Findings (bmad-code-review 2026-10-15)

Layer: Edge Case Hunter (keine Funde), Verification Gap (keine Lücken), Acceptance Auditor; Blind Hunter lief bereits im Build (oben).

- [x] [Review][Patch] Jahreswarnung nicht E2E getestet, 79-%-Kante nur unit [tests/test_e2e.py] — E2E Jahr 79/80 % ergänzt.
- [x] [Review][Patch] Kein Test, dass Tag/Woche nie warnen [tests/test_units.py] — `test_budget_warning_ignores_day_and_week`.

Rejected:
- H-1-Zeile der Spine außerhalb Intent geändert – low/informativ: H-1 ist in Commit 0746123 erledigt (`config.py` Defaults `off`/`false`), nötig für „Backlog abgeschlossen“.
- Webhook-IDs unter v6 gültig – maybe-false, nur gegen echtes Onshape prüfbar; Hinweis an Eric (erste `verify_webhook`-AUDIT-Zeile nach Deploy).
- Fake beantwortet weiter `POST /api/v6/webhooks/<id>` – low, harmloser Fixture-Zweig.
