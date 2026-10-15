---
reviews: ARCHITECTURE-SPINE.md (onshape-mcp-secure, 2026-10-15, draft)
lens: good-spine rubric
date: '2026-10-15'
verdict: 'Solid, codebase-faithful spine. Needs fixes before ratification: one fail-closed hole the code has today (WRITE_MODE default), AD-6 understates the KV debt, AD-8 is not enforced by config, and the operational envelope (backups, monitoring, secrets rotation, environments, rollback) is mostly undecided.'
---

# Review – Architecture Spine onshape-mcp-secure (Rubrik)

Geprüft gegen: `src/onshape_secure/*.py`, `fly.toml`, `Dockerfile`, `deploy/entrypoint.sh`, `README.md`, `CLAUDE.md`.

## Gesamturteil

Die Spine trifft das Paradigma (Policy-Enforcing Gateway um einen unveränderten Upstream) und ratifiziert die meisten bestehenden Entscheidungen korrekt. Viele Aussagen sind im Code nachweisbar (Guard-Reihenfolge Guard → reserve → refund in `_metered`, `hmac.compare_digest` im Webhook, WAL in `mapping.py:37`, loguru ohne `diagnose`/`backtrace`, `ADMIN_SYNC_COOLDOWN = 600`, `test_every_upstream_tool_is_classified`, nur `app.py` importiert `onshape_mcp`, TTLs 1 h / 1 Tag / 30 Tage).

Lücken: ein ADOPTED-Rule widerspricht dem Code (Fail-closed vs. `WRITE_MODE`-Default), AD-6 beschreibt die Altlast zu eng, AD-8 ist nur Konvention statt erzwungen, und die Betriebsdimension ist nur bei Deploy entschieden.

## Befunde

### F1 – HIGH – AD-5/AD-10: `WRITE_MODE` ist nicht fail-closed

- **Code:** `config.py:169` – `os.getenv("WRITE_MODE", "allowlist")`. Der Default ist `allowlist`, nicht `off`. `off` kommt allein aus `fly.toml [env]`.
- **Problem:** AD-10 sagt „Fail closed“, AD-5 sagt „`WRITE_MODE=off` in Produktion“. Wird die Zeile in `fly.toml` gelöscht oder umbenannt, oder läuft jemand lokal/anderswo ohne sie, sind Write-/Create-Tools freigeschaltet (Gate nur noch über Allowlist/Präfix `AI-`). Die Spine beschreibt den Ist-Zustand also falsch, und die Regel verhindert ihre Divergenz nicht.
- **Zusatz:** `fly.toml` setzt `ALLOW_DELETE_FEATURE=true` und `WRITE_ALLOWED_NAME_PREFIX=AI-` – latente Schreibpfade, die nur `WRITE_MODE` blockiert. AD-5 erwähnt das nicht.
- **Fix:** In AD-5/AD-10 festhalten: „Default jeder sicherheitsrelevanten Env-Variable ist die restriktivste Stufe (`WRITE_MODE` → `off`, `ALLOW_DELETE_FEATURE` → `false`)“. Code-Default in `config.py` auf `off` ändern (eigene `bmad-spec`), plus Test `test_write_mode_defaults_off`. `ALLOW_DELETE_FEATURE` in `fly.toml` auf `false` oder bewusst begründen.

### F2 – HIGH – Operativer Rahmen weitgehend unentschieden

Für die Altitude `initiative` mit Live-System fehlt fast alles außer Deploy:

| Dimension | Stand in Spine | Problem |
| --- | --- | --- |
| Backups / Volume-Snapshots | fehlt | `README.md:140` schlägt sogar vor, Snapshots abzuschalten. `usage.json` (Budgetzähler) und `mappings.db` (inkl. Webhook-Secret) liegen nur auf einem Volume. Verlust = Budgetzähler auf 0 → Jahresbudget kann überschritten werden; Mapping-Neuaufbau kostet Calls. |
| Monitoring / Alerting | fehlt | Keine Entscheidung zu Budget-Warnschwellen (z. B. 80 % Jahr), Healthcheck-Alarm, Webhook-Ausfall (`droppedEventCount`). |
| Logging / Retention | nur Format (AUDIT) | `fly logs` hat kurze Retention; AUDIT-Lines sind die einzige Nachvollziehbarkeit. Ob Logs exportiert/aufbewahrt werden, ist offen. |
| Secrets-Rotation | fehlt | Kein Ablauf für Rotation von `ONSHAPE_*`, `MCP_AUTH_TOKENS`, `ADMIN_TOKEN`, Webhook-Secret (liegt in `mappings.db.meta`, nicht in `fly secrets` – Konvention „Secrets nur über fly secrets“ stimmt dafür nicht). `fly.toml`-Kommentar nennt `ADMIN_TOKEN` nicht. |
| Umgebungen | implizit nur Prod | Keine Aussage, ob es Staging gibt; Tests laufen nur gegen Fake-Onshape. Muss mindestens als „bewusst nur Prod“ entschieden werden. |
| Rollback | fehlt | Kein Weg (`fly releases` / `fly deploy --image <vorher>`); Schema-Migration von `mappings.db` nicht geregelt. |
| Dependency-/Base-Image-Pflege | fehlt | Digest und Hashes sind gepinnt, aber kein Rhythmus für Security-Updates. |

- **Fix:** Neuer Abschnitt „Operational Envelope“ (oder AD-12) mit: Snapshots **an** (täglich, ≥5 Tage, nicht abschaltbar ohne AD-Änderung) und dokumentierter Restore; Budget-Alarm-Schwelle; Log-Aufbewahrung (entscheiden oder explizit deferred mit Begründung); Rotationsablauf je Secret; „nur Prod, kein Staging“ als bewusste Entscheidung; Rollback-Befehl; Update-Rhythmus Base-Image/Deps. Die README-Empfehlung zum Abschalten der Snapshots mit dem AD abgleichen.

### F3 – MEDIUM – AD-8 „genau eine Instanz“ ist nicht erzwungen, In-Prozess-Zustand überlebt Auto-Stop nicht

- **Code/Config:** `fly.toml` hat `auto_start_machines = true`, `min_machines_running = 0`, aber keine Obergrenze; `--ha=false` wirkt nur beim Deploy-Aufruf. `fly scale count 2` oder ein Deploy ohne `--ha=false` erzeugt eine zweite Maschine (ohne Volume schlägt sie zwar fehl oder läuft ohne `/data` → Caches/Mapping deaktiviert, siehe `app.py` „Persistent cache disabled“-Pfad – also degradiert statt Fehler).
- **Problem:** AD-8 erlaubt, dass Rate-Limiter und Sync-Cooldown im Prozess liegen. Durch Auto-Stop setzt jeder Neustart den 10-Minuten-Cooldown von `/admin/sync` und den Rate-Limiter zurück. Damit hält CLAUDE.md „max. einmal pro 10 min“ nicht hart.
- **Fix:** AD-8 ergänzen: (a) Start verweigert, wenn `/data` nicht beschreibbar ist (fail closed statt degradiert) – mindestens für `usage.json`; (b) Sync-Cooldown-Zeitstempel in `mappings.db.meta` persistieren; (c) Deploy-Befehl mit `--ha=false` als einzige erlaubte Form festschreiben (oder Fly-Konfig, die die Maschinenzahl begrenzt).

### F4 – MEDIUM – AD-6 unterschätzt die KV-Altlast und lässt den Degradationsmodus offen

- **Code:** Teile-Daten im `KVCache` nicht nur in `ExtraTools._revision` (`extra_tools.py:291-296`), sondern auch in `_drawings_where_used` (`extra_tools.py:746/781`, Teil→Zeichnung-Links, Fallback wenn `mapping is None`) und `_locate_by_where_used` (`wu_pn_miss:<partNumber>`, `extra_tools.py:409-430`, Teilenummer-Not-Found).
- **Ursache:** `app.py` erlaubt `mapping = None`, wenn `MappingStore` nicht startet („Mapping table disabled“). Die KV-Pfade sind der Fallback dafür. Entfernt man sie nach AD-6, ohne den Degradationsmodus zu entscheiden, holt jeder Lookup ohne Mapping-DB live → Budgetrisiko (AD-4).
- **Fix:** AD-6-Regel erweitern: alle drei Stellen nennen (oder „jeder `kv`-Zugriff mit Teilenummer/Revision im Schlüssel“) und festlegen: „Ohne Mapping-DB startet der Server nicht“ (fail closed) **oder** „Teile-Tools sind ohne Mapping-DB deaktiviert“. `wu_pn_miss` entweder in die Mapping-DB (Not-Found-Tabelle) verschieben oder explizit als zulässigen nicht-fachlichen Negativ-Cache ausnehmen.

### F5 – MEDIUM – AD-3 beschreibt die Ausnahme `fetch_document_name` nicht

- **Code:** `app.py:218-226` – `fetch_document_name` ruft `raw_get` (ungewrappt) mit `budget.reserve()` + `_billed`, aber **ohne** `check_onshape_request`. Das ist GET, also ungefährlich, aber die Regel „Jeder Request ruft `check_onshape_request` auf“ stimmt so nicht, und „kein `raw_get` außerhalb von `app.py`“ impliziert, dass innerhalb erlaubt ist – ohne Bedingung.
- **Fix:** Regel präzisieren: „`raw_get` nur in `app.py`, nur für GET, nur mit `reserve`+`_billed`“, oder `fetch_document_name` über den gewrappten Client führen. Optional ein Test, der per AST/Grep `httpx.AsyncClient`/`_client.stream` außerhalb von `app.py` verbietet – sonst ist AD-3 nicht maschinell durchsetzbar.

### F6 – LOW – Konvention „AUDIT als JSON“ stimmt für Admin-Aktionen nicht

- **Code:** `_audit()` schreibt JSON (`app.py:80-82`), aber Admin-Endpunkte loggen Freitext: `logger.info(f"AUDIT admin_webhook method=...")` (`app.py:505`), `AUDIT admin_sync query=...` (`app.py:574`).
- **Fix:** Admin-Lines auf `_audit(...)` umstellen (kleiner Fix) oder Konvention auf „Tool-Calls JSON, Admin Freitext“ abschwächen. Ersteres ist besser für spätere Log-Auswertung.

### F7 – LOW – AD-2 Klassenliste unvollständig

- **Code:** `policy.py:19` definiert zusätzlich `SYSTEM` (Webhook-Verwaltung, von Tools nie erreichbar). AD-5 nennt `system`, AD-2 nicht. Zudem gibt es `settings.read_restricted`, das Listen-/Such-Tools sperrt – ein weiterer Freigabe-Schalter, den AD-2 nicht erwähnt.
- **Fix:** AD-2: „`system` ist reserviert und darf nie in `TOOL_CLASSES` stehen“ (+ Test), und `read_restricted`/`disabled_tools` als weitere Gates aufführen.

### F8 – LOW – AD-4 Budget firmenweit vs. Server-Limit

- Server-Jahreslimit 2.400 von 2.500 firmenweit. Ob andere Onshape-API-Nutzer (andere Integrationen, Skripte) dasselbe Kontingent verbrauchen, ist nicht entschieden. Der Server zählt nur sich selbst.
- **Fix:** Eine Zeile in AD-4: „Dieser Server ist der einzige API-Key-Konsument“ oder „Reserve von 100 für andere Nutzer; Eric verwaltet sie“.

### F9 – LOW – Deferred „Legacy-SSE abschalten“ und Upstream-Update

- SSE: unkritisch, da AD-8 eine Instanz erzwingt (SSE braucht Sticky Sessions). Sollte als Abhängigkeit an AD-8 vermerkt sein.
- Upstream-Update: Prozess (komplett kopieren + beide Suiten) ist in AD-1 entschieden; es fehlt „neue Upstream-Tools landen unklassifiziert und damit gesperrt“ als Schritt – folgt zwar aus AD-2, ein Hinweis im Ablauf schadet nicht. Ebenso Security-Review des Upstream-Diffs (Upstream läuft mit den Onshape-Keys).
- Keiner der Deferred-Punkte lässt zwei Einheiten divergieren, **sofern** AD-6 (F4) und AD-11 präzisiert sind. AD-11-Zwischenzustand („jedes neue Tool in `TOOLS`, `TOOL_CLASSES`, `EXTRA_TOOL_NAMES`, `handler()`“) ist durchsetzbar, wenn ein Test die vier Listen abgleicht – heute deckt `test_every_upstream_tool_is_classified` nur Upstream-Tools ab. Prüfen, ob es einen Gegenpart für Extra-Tools gibt; falls nicht, Test ergänzen.

## Checkliste

| Kriterium | Ergebnis |
| --- | --- |
| Reale Divergenzpunkte fixiert, keine fehlt | Weitgehend ja. Fehlend: Degradationsmodus ohne `/data`/Mapping (F3, F4), Secret-Defaults (F1), Schema-Migration `mappings.db`. |
| Jede Rule durchsetzbar und verhindert ihre Divergenz | AD-1, AD-2, AD-7, AD-9 ja. AD-3 nur per Review (F5). AD-5/AD-10 nicht (F1). AD-8 nicht erzwungen (F3). AD-11 nur mit Abgleich-Test (F9). |
| Deferred lässt nichts divergieren | Ja, unter Vorbehalt F4 (KV-Fallback-Entfernung braucht Entscheidung zum Degradationsmodus vorher). |
| Ratifiziert statt widerspricht dem Code | Überwiegend. Widersprüche: F1 (Default `allowlist`), F4 (mehr KV-Altlast als genannt), F5 (Guard nicht bei jedem Request), F6 (Admin-AUDIT kein JSON), Webhook-Secret liegt in DB statt `fly secrets` (F2). |
| Alle Dimensionen entschieden/deferred/offen | Nein: Backups, Monitoring/Alerting, Log-Retention, Secrets-Rotation, Umgebungen, Rollback, Dependency-Pflege fehlen (F2). |
