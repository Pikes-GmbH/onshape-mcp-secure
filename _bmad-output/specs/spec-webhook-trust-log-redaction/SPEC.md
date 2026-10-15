---
id: SPEC-webhook-trust-log-redaction
companions:
  - ../../planning-artifacts/architecture/architecture-onshape-mcp-secure-2026-10-15/ARCHITECTURE-SPINE.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# H-4 + H-8: Webhooks erhöhen kein Vertrauen, Logs zeigen keine Geheimpfade

## Why

Architektur-Backlog H-4 (AD-7) und H-8 (AD-10, AD-13). Heute verlängert ein `webhook.ping`/`webhook.register` an den Geheimpfad den Webhook-Status (`webhook_verified_at`) und damit die 30-Tage-TTL; Events ohne `webhookId` werden auch nach der Registrierung verarbeitet; Release-Events lassen den Working-State-Index (`ws_parts` Status/Revision) bis zu 30 Tage veraltet. `auth_failed` loggt den vollen Pfad – bei falsch adressierten Requests also Webhook-Geheimnis oder Link-Token.

## Capabilities

- **CAP-1**
  - **intent:** Webhook-Payloads setzen nie `webhook_verified_at`. Nur die Registrierung in `/admin/webhook` und der gemeterte Wochen-GET (`_verify_webhook_if_due`) dürfen es.
  - **success:** Nach `webhook.ping`/`webhook.register` an den Geheimpfad ist `webhook_verified_at` unverändert (auch wenn leer oder abgelaufen); direkt nach Registrierung ist `active` weiter `true`.
- **CAP-2**
  - **intent:** Sobald eine Webhook-ID gespeichert ist, werden Events mit fehlender oder unbekannter `webhookId` ignoriert.
  - **success:** Solch ein Event liefert 200, invalidiert nichts und schreibt keine Meta-Werte; Events mit bekannter ID wirken wie bisher; vor der Registrierung werden Events weiter verarbeitet.
- **CAP-3**
  - **intent:** Release-Events (`onshape.revision.created`, `onshape.workflow.transition`) markieren zusätzlich das betroffene Dokument in `ws_docs` dirty.
  - **success:** Event mit gültiger `documentId` (24 Hex) → nächster Lookup indexiert genau dieses Dokument neu; ohne/ungültige `documentId` wird kein `ws_docs`-Eintrag dirty.
- **CAP-4**
  - **intent:** `auth_failed` loggt Pfade unter `/onshape/webhook/`, `/img/`, `/dl/` nur mit den ersten 6 Zeichen von Geheimnis/Token.
  - **success:** Fehlgeschlagener Request auf diese Pfade → Log enthält Präfix + 6 Zeichen, nie das volle Geheimnis/Token; andere Pfade werden wie bisher (escaped, max. 200 Zeichen) geloggt. Uvicorn-Access-Log ist explizit aus.

## Constraints

- Webhook-Antwort bleibt 200 für ignorierte Events (Onshape soll nicht neu senden oder deaktivieren).
- Ein Release-Event ohne `documentId` markiert nicht alle Dokumente dirty: das erzwänge Re-Index aller Dokumente (API-Budget).
- Metadaten-Event: `documentId` fehlt/leer → alle Dokumente (wie bisher); vorhanden, aber ungültig → keins.
- Webhook-Logzeilen escapen Payload-Strings (kein Log-Forging) und zeigen `doc=yes/no`, um die `documentId`-Annahme in Produktion zu prüfen.
- Keine zusätzlichen Onshape-Calls; kein Edit in `src/onshape_mcp/`.
- Schmal halten: spätere Pakete ändern `policy.py`, `extra_tools.py`, Call-Scope in `app.py`.

## Non-goals

- Keine HMAC-Signaturprüfung (Deferred in der Spine).
- Keine Änderung an TTL-Werten oder am Wochen-GET.
- Kein Deploy, kein Push, kein Commit ohne Erics OK.

## Success signal

- `python -m pytest -q tests` grün inkl. neuer Tests für CAP-1..CAP-4; H-4 und H-8 in der Spine als erledigt markiert.

## Assumptions

- Onshape-Payloads für `onshape.revision.created` tragen `documentId` (wie Metadaten-Events).
- `image_served`/`download_served` loggen schon 6-Zeichen-Präfixe – `auth_failed` war die einzige Stelle mit vollem Pfad.
