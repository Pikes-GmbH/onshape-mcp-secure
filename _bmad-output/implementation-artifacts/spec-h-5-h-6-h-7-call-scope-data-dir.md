---
title: 'H-5 + H-6 + H-7: Ein Call-Scope für jeden Onshape-Zugriff, einheitliches Audit, Start nur mit beschreibbarem /data'
type: 'bugfix'
created: '2026-10-15'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-call-scope-data-dir/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `fetch_document_name` umgeht über `raw_get` den HTTP-Guard, nur get/post/delete des Upstream-Clients sind gewrappt (H-5, AD-3). Admin-Endpunkte loggen Freitext-`AUDIT`, Kosten erscheinen mal als Requests, mal als billed, der `/admin/sync`-Cooldown lebt nur im Prozess (H-6, AD-3, AD-8). Ohne beschreibbares Datenverzeichnis startet der Server und schaltet Mapping/Caches still ab (H-7, AD-8).

**Approach:** `app.py`: ein Helfer `_send` (Guard → Budget-Reserve → Request → Refund bei Fehler) für alle gewrappten Client-Methoden, `get_bytes` und `fetch_document_name`; Request ohne Call-Scope → `PolicyDenied`. Start bricht ab, wenn der Upstream-Client eine öffentliche Coroutine hat, die weder gewrappt noch `close` ist. Ein `_call_scope`-Kontextmanager (Akteur, Aktion, Klasse, Dokument) für Tools, `/admin/webhook`, `/admin/sync` und den Webhook-Check-Job schreibt genau eine JSON-`AUDIT`-Zeile mit `onshapeCalls` (billed) und `attempts`; Job-Calls zählen zusätzlich in der Verbrauchszeile des Tools. Admin-Antworten melden `onshapeCalls`. Cooldown in `mappings.db` `meta` (`admin_sync_last`). Datenverzeichnis per Probe-Datei prüfen; Fehler beim Öffnen von Mapping-DB, KV-/Datei-Cache, Bild-/Link-Speicher → `DataDirError(ConfigError)`, `main()` Exit 2. Tests je Verhalten; Spine H-5/H-6/H-7 erledigt; README/DOKUMENTATION zum Audit-Format. Kein Commit ohne Erics OK.

</frozen-after-approval>

## Implementation Notes

- `app.py` H-5: `_send(method, path, call)` ist der einzige Weg zu Onshape (Call-Scope-Pflicht → `check_onshape_request` → `budget.reserve` → Refund bei 4xx/5xx/Transportfehler). `_metered` wrappt jede Methode aus `CLIENT_REQUEST_METHODS` (get/post/put/patch/delete, soweit vorhanden) und markiert sie `secure_metered`; `_check_client_methods` bricht bei jeder anderen öffentlichen Methode außer `close` ab (`RuntimeError` mit Namen). Originale werden aus der Klasse gebunden (`types.MethodType`), nicht aus der Instanz – der alte `_secure_original_methods`-Cache entfällt. `fetch_document_name` nutzt den gewrappten `get`, `get_bytes` ruft `_send("get", …)`.
- H-6: `_call_scope(event, action, cls, document)` (Modulebene) setzt den Kontext, schreibt beim Verlassen genau eine `_audit`-Zeile (`event`, `client`, `action`, `cls`, `decision`, `document`, `onshapeCalls`=billed, `attempts`, `blocked`, `ms`, plus `ctx["audit"]`). `call_tool` → `_run_tool` im Scope `tool_call`; `/admin/webhook` und `/admin/sync` im Scope `admin` inkl. 403/400/429-Ablehnungen (vorher Freitext `admin_denied`/`AUDIT admin_…`); `_verify_webhook_if_due` im verschachtelten Scope `job` (Klasse `system`), dessen Calls über `nested_billed` in der Verbrauchszeile des Tools erscheinen, nicht in dessen `onshapeCalls` (keine Doppelzählung im Log). `/admin/webhook` antwortet `onshapeCalls` statt `onshapeRequests`. Registrierung in `_register_webhooks` ausgelagert.
- Cooldown: Meta-Schlüssel `admin_sync_last`, gesetzt vor dem Lauf (auch bei Fehlschlag, wie bisher); Startzeit in der Zukunft (Uhrsprung) wird ignoriert, sonst wäre Sync dauerhaft gesperrt.
- H-7: `_check_data_dir` (Probe-Datei anlegen/löschen) vor Budget und Stores; `_open_store` macht aus jedem Öffnungsfehler (KV, Datei-Cache, Bild-/Link-Speicher, Mapping-DB) `DataDirError(ConfigError)` → `main()` Exit 2. `mapping` ist damit nie `None`; Webhook/Admin-Routen hängen nur noch an `PUBLIC_BASE_URL`.
- Tests: Unit – alle öffentlichen Client-Methoden gewrappt, neue Methode stoppt Start, neues Verb (`put`) wird gewrappt, Request ohne Scope abgelehnt (kein Request, kein Budget), fehlendes/als Datei blockiertes Datenverzeichnis, kaputte Mapping-DB, nicht öffnbarer Bildspeicher, `main()` Exit 2, keine Probe-Datei bleibt liegen, kein Freitext-`AUDIT` im Code. E2E – Namens-Lookup gemetert (Audit = Fake-Requests = Verbrauchszeile = Budget), 404 zählt als `attempts`, nicht `onshapeCalls`, Admin-Audit-Sequenz als JSON, Cooldown über Neustart, Zukunfts-Startzeit, Job-Audit + Verbrauchszeile.
- Lokale Umgebung: Windows-venv braucht zusätzlich `pywin32`, `win32-setctime`, `colorama` (requirements.txt ist Linux-gehasht); nur Dev, nicht im Image.
- Doku: README (Audit-Felder, Datenverzeichnis, Cooldown), DOKUMENTATION, Spine H-5/H-6/H-7 erledigt; Volume-Mount-Erkennung als deferred.


## Review Triage Log

Oneshot-Layer Blind Hunter übersprungen, weil `bmad-code-review` (4 Layer: Blind Hunter, Edge Case Hunter, Verification Gap, Acceptance Auditor) direkt folgte. Patches angewendet (headless, Auftrag aus Delegations-Session). Gegenprobe vor dem Review: 15 neue Tests schlagen gegen den alten `app.py` fehl.

- Cooldown-Schlüssel `admin_sync_started_at` ≠ Intent `admin_sync_last` (BH, ECH, AA) — medium (Vertrag) → patch: Schlüssel `admin_sync_last`.
- Docstrings enthielten „AUDIT “, Freitext-Test hatte Ausnahme „AUDIT line“ (BH, AA) — low, direkte Korrektur → patch: Docstrings umformuliert, Test verlangt genau eine `AUDIT`-Stelle (in `_audit`).
- `**ctx["audit"]` konnte feste Felder kollidieren lassen (TypeError, Audit verloren) (BH) — low, direkte Korrektur → patch: Extras zuerst, feste Felder überschreiben.
- `/admin/sync` sonstige Fehler → nackter 500 ohne Fehlertyp im Audit (BH, VG) — medium → patch: 502 JSON mit `error`/`onshapeStatus`/`onshapeCalls` wie `/admin/webhook` + Test.
- `get_bytes`-Metering und Refund ungetestet (VG) — medium → patch: `test_export_download_is_metered`, `test_failed_download_and_transport_error_are_refunded` (deckt auch Transportfehler-Refund, AA/BH).
- Webhook-404-Job-Pfad ungetestet (VG) — medium → patch: `test_vanished_webhook_job_audits_error_and_falls_back`.
- Admin-Fehlerpfade (keine Webhook-ID, 500, Budget leer, 400er) ungetestet (VG, BH) — medium → patch: drei parametrisierte Tests.
- Direkter Zugriff auf `client._client` / private Helfer umgeht den Wrapper (BH) — medium (heute kein Treffer im Upstream) → patch: `test_nothing_reaches_onshape_around_the_client_methods` scannt `src/` (nur `client.py` und `get_bytes` erlaubt).
- Tests leakten globalen Client-Zustand (`_client`, `put`) (BH, VG) — low, direkte Korrektur → patch: `monkeypatch`/`del`.
- README nannte Umbenennung der Audit-/Admin-Felder nicht (BH) — low, Doku → patch.
- `_check_client_methods` prüft alle öffentlichen Callables statt nur Coroutinen (BH) — false als Defekt: bewusst strenger (fail closed); ein Upstream-Update läuft vorher durch die Tests.
- Unbekannte Client-Methode → `RuntimeError` statt Exit 2 (AA) — low: Code-Fehler beim Upstream-Update, nicht Konfiguration; Traceback ist dort gewollt → abgelehnt.
- Job-Audit trägt Client-Label des auslösenden Tool-Calls (BH) — false: das ist der Akteur, der den Job ausgelöst hat; `event: job` unterscheidet.
- `/admin/sync` setzt Cooldown und synct vor Prüfung von `document` (BH) — false als Regression: vorbestehende Reihenfolge, und Dokumentnamen kommen erst durch den Sync in die Tabelle.
- Kaputtes `cache.json` stoppt den Start nicht (ECH) — false als Defekt: KV hält nur neu ladbare unveränderliche Metadaten und startet leer; Beschreibbarkeit prüft die Probe.
- Schreibgeschütztes `usage.json` in beschreibbarem Verzeichnis (BH, ECH) — false für Linux: `_save` schreibt tmp + `os.replace`, das braucht nur Verzeichnisrechte. Kaputtes `usage.json` → Zähler 0: vorbestehend → defer.
- Volles Volume (ENOSPC) verhindert Start vor dem Aufräumen (ECH) — maybe-false/medium, unwahrscheinlich → defer.
- Eigenes `IMAGE_DIR` nicht per Probe geprüft (AA) — low, in Produktion nicht gesetzt → abgelehnt.
- Abschluss: 223 Tests + 544 Upstream grün.
