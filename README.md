# Onshape MCP – gehärtete Remote-Version für ClickUp

Remote-MCP-Server (HTTPS), der ClickUp-Brain und Super Agents Zugriff auf Onshape gibt – Lesen **und** Modellieren, aber abgesichert.
Die eigentlichen Onshape-Tools stammen unverändert aus [hedless/onshape-mcp](https://github.com/hedless/onshape-mcp) (MIT, Commit `a9c2a5e`, siehe `NOTICE`). Darum herum liegt eine Sicherheitsschicht.

**Anwender- und Betriebsdokumentation:** [docs/DOKUMENTATION.md](docs/DOKUMENTATION.md)

## Was gegenüber dem Original abgesichert ist

| Problem im Original | Lösung hier |
|---|---|
| Nur lokal (stdio), keine Authentifizierung | HTTPS-Endpunkt `/mcp`, Bearer-Token Pflicht (min. 32 Zeichen, Vergleich in konstanter Zeit) |
| `create_document` legt standardmäßig **öffentliche** Dokumente an | Immer privat erzwungen, Parameter entfernt |
| Screenshot-Tools schreiben Dateien an beliebige Pfade auf dem Server (`outputPath`) | Parameter entfernt und verworfen |
| `delete_feature` setzt die Feature-ID ungeprüft in die URL (Path-Traversal → z. B. `DELETE /documents/...`) | Strikte ID-Prüfung aller Parameter |
| KI darf in **jedes** Dokument schreiben | Schreiben nur in Dokumenten mit Namens-Präfix `AI-` oder per ID freigegeben |
| `get_face_coordinate_system` legt trotz Lese-Namen temporär Features an – in jedem Dokument | Als Schreib-Tool eingestuft. Zusätzlich blockt ein HTTP-Wächter **jede** POST/DELETE-Anfrage an Onshape, die nicht exakt das freigegebene Dokument betrifft – unabhängig vom Upstream-Code |
| Loguru-Diagnose schreibt bei Netzwerkfehlern den Onshape-API-Key ins Log | Diagnose/Backtrace abgeschaltet, per Test abgesichert |
| Kein Schutz des Onshape-API-Kontingents (Onshape sperrt nach Jahreslimit mit HTTP 402) | Tages-/Monatsbudget (zählt jeden Versuch, atomar, persistent gespeichert) + Rate-Limit pro Client |
| DEBUG-Logs mit CAD-Daten | INFO-Logs + strukturiertes Audit-Log jeder Tool-Ausführung |

Weitere Schichten: max. 10 parallele SSE-Sitzungen, an das Token gebunden; WebSockets abgewiesen; Auth-Fehler-Logs escaped und gedrosselt; unbekannte/neue Upstream-Tools werden nie freigegeben (Fail-closed), Längen-/Zahlenlimits für alle Argumente, unbekannte Argumente werden verworfen, Antwortgröße begrenzt, Timeout 180 s, Container ohne Root-Rechte (keine Capabilities, `no-new-privs`), Abhängigkeiten per SHA-256-Hash gepinnt, Basis-Image per Digest gepinnt.

## Sicherheitsmodell in einem Satz

ClickUp → (Token) → dieser Server → (Policy: Präfix/Allowlist, Budget) → Onshape mit einem **eigenen Service-Account**, der nur die Dokumente sieht, die ihr mit ihm teilt.

> Wichtig: Alle ClickUp-Mitglieder mit Zugriff auf die Workspace-Verbindung sehen alles, was der Service-Account sehen darf. Teilt mit ihm nur, was die KI sehen soll.

---

## Einrichtung (ca. 20 Minuten)

### 1. Onshape-Service-Account + API-Key

1. Eigenen Onshape-Benutzer anlegen (z. B. `ki-assistent@pikes.de`) – im bezahlten Firmen-Plan, sonst kann er keine privaten Dokumente anlegen.
2. Mit diesem Account: *My account → Developer → API keys → Create new API key*
   - Berechtigungen **nur**: `Application can read your documents` und `Application can write to your documents`
   - **Nicht** anhaken: Delete, Share, Purchase, Profile
   - TTL aktivieren (z. B. 12 Monate)
3. Access Key + Secret Key notieren (Secret wird nur einmal angezeigt).
4. Gewünschte Dokumente mit dem Service-Account teilen – bestehende Konstruktionen am besten nur **„Can view“**. Dann ist Schreiben dort zusätzlich durch Onshape selbst blockiert.

Enterprise-Stack (`firma.onshape.com`)? Key dort anlegen und `ONSHAPE_BASE_URL` setzen.

### 2. Fly.io deployen

```bash
# einmalig: https://fly.io/docs/flyctl/install/
fly auth login

# in diesem Ordner; App-Name in fly.toml ggf. anpassen (muss global eindeutig sein)
fly apps create pikes-onshape-mcp
fly volumes create onshape_mcp_data --region fra --size 1 -y

TOKEN=$(openssl rand -hex 32); echo "ClickUp-Token: $TOKEN"   # notieren!
fly secrets set \
  ONSHAPE_ACCESS_KEY="..." \
  ONSHAPE_SECRET_KEY="..." \
  MCP_AUTH_TOKENS="clickup:$TOKEN"

fly deploy --ha=false
```

Test:

```bash
curl https://pikes-onshape-mcp.fly.dev/healthz        # {"status":"ok"}
curl -X POST https://pikes-onshape-mcp.fly.dev/mcp \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_server_policy","arguments":{}}}'
```

### 3. In ClickUp einbinden

Nur Workspace-Owner/Admins, nur in der Browser-App:

1. *App Center → MCP Servers / Custom MCP → Add New*
2. Tab **Workspace** (für alle) wählen → *Next*
3. Name `Onshape`, URL `https://pikes-onshape-mcp.fly.dev/mcp`
4. Authentifizierung **API key** → Token einfügen
5. Unter *AI Tools* prüfen, dass die Tools erscheinen
6. Im Super Agent: *Tools → Add tools → Custom MCP Server → Onshape*

Falls die Verbindung scheitert:
- Auth auf *No authentication* stellen und Custom Header `Authorization: Bearer <TOKEN>` (alternativ `X-API-Key: <TOKEN>`) setzen.
- Falls ClickUp nur SSE spricht: URL `https://…/sse` verwenden (braucht `ENABLE_LEGACY_SSE=true`, in `fly.toml` gesetzt).

### 4. So arbeitet die KI damit

- **Neues Teil bauen:** „Erstelle in Onshape ein Dokument *Halter Akku* und modelliere …“ → `create_document` legt `AI-Halter Akku` (privat) an, darin darf modelliert werden.
- **Bestehendes Dokument freigeben:** Dokument in Onshape umbenennen auf `AI-…` **oder** ID in `WRITE_ALLOWED_DOCUMENT_IDS` eintragen.
- **Alles andere:** nur lesen (Teile, Baugruppen, Variablen, Screenshots, Interferenz-Check).
- Fehlgriffe lassen sich in Onshape über die Versionshistorie (*History → Restore*) zurücksetzen.

---

## Zusätzliche Tools (nicht im Original) – auf minimale API-Aufrufe ausgelegt

| Tool | Zweck | Onshape-Aufrufe (erstes Mal → danach) |
|---|---|---|
| `find_part_by_number` | Freigegebenes Teil/Baugruppe/Zeichnung per Teilenummer finden | 1 → 0 (Tabelle) |
| `capture_part_screenshot` | Bild genau eines Teils (oder einer freigegebenen Baugruppe) | 2 → 0 (24 h) |
| `export_released_model` | STEP eines freigegebenen Teils/einer Baugruppe als Download-Link | ~4 → 0 (24 h) |
| `export_released_drawing` | Zeichnung als PDF/DXF/DWG als Download-Link | ~6 → 0 (24 h) |
| `find_drawing_for_part` | Welche Zeichnung (Nummer, Revision) gehört zum Teil? | 2 → 0 (Tabelle) |
| `find_part_by_number` (nicht freigegeben) | Teilenummer im Arbeitsstand über den Teilenummern-Index | 2 pro Dokument (einmalig) → 0 |
| `get_parts` (ersetzt Original) | Teile eines Part Studios inkl. Teilenummer | 1 |
| `show_mapping_table` | Gespeicherte Zuordnungen anzeigen | 0 |
| `search_part_index` | Teile im Index nach Name/Teilenummer-Fragment suchen | 0 |

So wird gespart:
- **Zuordnungstabelle** (`/data/mappings.db`, SQLite): Teilenummer + Revision → Onshape-IDs, „letzte Revision“, Teil → Zeichnung (z. B. PRT-000593 Rev A → DRW-000635 Rev A). Feste Revisionen gelten für immer, sie ändern sich nie. Ansehen ohne API-Aufruf: Tool `show_mapping_table`.
- **Freigabe-Webhook**: Onshape meldet jede neue Freigabe an `/onshape/webhook/<geheim>`; der Server verwirft dann gezielt die betroffenen Einträge. Dadurch bleibt „letzte Revision“ ohne Nachfragen aktuell (Meldungen zählen nicht als API-Aufruf). Ohne Webhook: Neuprüfung nach 1 h. Einmal pro Woche prüft der Server mit 1 Aufruf, ob der Webhook noch lebt.
- **Große Dateien** (STEP, PDF, Renderings) liegen nur **24 h** auf dem Volume (`FILE_CACHE_HOURS`), dann werden sie gelöscht – auch ohne erneuten Abruf (Aufräumen alle 30 min und bei jedem Start). Download-/Bild-Links ebenfalls 24 h.
- Exporte: erste Statusabfrage erst nach 5 s (meist schon fertig), danach wachsende Abstände. Dauert es länger als ~1 min, liefert das Tool „läuft noch“ und setzt beim nächsten Aufruf **denselben** Export fort statt neu zu starten. Gleichzeitige identische Anfragen teilen sich einen Export.
- **Nicht freigegebene Teile finden:** zuerst Where Used firmenweit per Teilenummer (`/api/v17/productstructure/whereused?partNumber=…&useLatestVersion=false`, 1 Aufruf) → liefert das Dokument des Teils, das dann einmalig indexiert wird. Klappt für alle Teile, die in mindestens einer Baugruppe verbaut sind; sonst Dokument angeben.
- **Teilenummern-Index für nicht freigegebene Teile:** pro Dokument 1 Aufruf (`/parts/d/…/w/…` liefert alle Teile aller Part Studios inkl. Teilenummer), gespeichert in der Tabelle. Indexiert werden Dokumente, in denen schon freigegebene Teile liegen, plus vom Nutzer genannte. Der Webhook `onshape.model.lifecycle.metadata` markiert geänderte Dokumente als veraltet; ohne Webhook täglich neu.
- **Budget wie Onshape abrechnet:** Fehlgeschlagene Aufrufe (4xx/5xx) werden zurückgebucht, Onshape berechnet sie nicht. Limits: 500/Tag, 600/Woche, 1000/Monat, 2400/Jahr (Plan: 2500/Jahr).
- **Kosten sichtbar:** Jedes Tool-Ergebnis endet mit `[Onshape-API: n Aufruf(e) für diese Anfrage · heute … · Woche … · Monat … · Jahr …]`; die KI soll die Summe am Ende ihrer Antwort nennen.
- Die Server-Anweisungen verbieten der KI „Erkunden“: Teilenummern immer über `find_part_by_number`; wird nichts gefunden, muss sie den Nutzer nach dem Dokument fragen.

**Batch-Aufbau der Tabelle (Admin):**

```bash
# alle aktuellen Freigaben der Firma (1 Aufruf je 100 Revisionen, danach inkrementell)
curl -X POST "https://pikes-onshape-mcp.fly.dev/admin/sync" -H "Authorization: Bearer $ADMIN_TOKEN"
# zusätzlich Teil -> Zeichnung für ein Dokument (Where Used, ~1 Aufruf pro freigegebenem Teil)
curl -X POST "https://pikes-onshape-mcp.fly.dev/admin/sync?document=Robot%20Gen3&maxParts=50" -H "Authorization: Bearer $ADMIN_TOKEN"
```

**Webhook einrichten (einmalig):**

```bash
curl -X POST https://pikes-onshape-mcp.fly.dev/admin/webhook -H "Authorization: Bearer $ADMIN_TOKEN"
curl https://pikes-onshape-mcp.fly.dev/admin/webhook -H "Authorization: Bearer $ADMIN_TOKEN"   # Status
```

Hinweis: Fly-Snapshots des Volumes (täglich, 5 Tage) enthalten auch Dateien, die zum Zeitpunkt des Snapshots < 24 h alt waren. Wer das nicht will: `fly volumes update <id> --scheduled-snapshots=false`.

Zeichnung finden: per **Where Used** auf die Freigabeversion des Teils (1 Aufruf), dann die eigene Freigabe der Zeichnung (z. B. DRW-000635 Rev A, 1 Aufruf) – genau wie in der Onshape-Oberfläche. Fallbacks: Freigabepaket, Zeichnungs-Tabs im Dokument. Mehrere Zeichnungen → die KI bekommt eine Auswahl, alternativ direkt `drawingNumber`.

Nicht freigegebene Teile sind über die Onshape-API nicht per Teilenummer auffindbar.

**Bilder & Dateien in ClickUp:** Screenshots als Markdown-Bild, Exporte als Download-Link – jeweils zufällige, 24 h gültige Links (`/img/…`, `/dl/…`). Wer den Link kennt, kann ohne Token laden; deshalb kurze Laufzeit, nicht erratbar, danach gelöscht. PDFs öffnen im Browser, STEP wird heruntergeladen. Abschalten mit `IMAGE_LINKS=false` (dann auch keine Exporte).

## Konfiguration

| Variable | Standard | Bedeutung |
|---|---|---|
| `ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY` | – | **Secret.** API-Key des Service-Accounts |
| `MCP_AUTH_TOKENS` | – | **Secret.** `label:token,label2:token2` – Label erscheint im Audit-Log (Label `admin` ist reserviert) |
| `ADMIN_TOKEN` | – | **Secret.** Eigenes Token nur für `/admin/*` (min. 32 Zeichen). Client-Tokens wie das von ClickUp erhalten dort 403; `/admin/sync` höchstens alle 10 Minuten (auch über Neustarts) |
| `WRITE_MODE` | `off` | `off` = nur lesen, `allowlist` = Präfix/IDs, `all` = überall (nicht empfohlen) |
| `WRITE_ALLOWED_NAME_PREFIX` | `AI-` | Dokumente mit diesem Namensanfang sind beschreibbar; leer = aus |
| `WRITE_ALLOWED_DOCUMENT_IDS` | leer | Zusätzlich beschreibbare Dokument-IDs (kommagetrennt) |
| `READ_ALLOWED_DOCUMENT_IDS` | leer | Wenn gesetzt: Lesen nur dort (+ beschreibbare); dokumentübergreifende Tools (Liste/Suche, Mapping-Tabelle, Teile-Index) werden ausgeblendet, Treffer aus anderen Dokumenten weggelassen |
| `ALLOW_DELETE_FEATURE` | `false` | Features löschen (nur in beschreibbaren Dokumenten) |
| `ALLOW_FEATURESCRIPT` | `false` | `eval_featurescript` (von Onshape nur lesend ausgeführt); Produktion: `true` |
| `DISABLED_TOOLS` | leer | Zusätzlich ausgeblendete Tools; unbekannte Namen verhindern den Start. `export_part_studio` und `export_assembly` sind immer gesperrt (Ergebnis nicht über ClickUp herunterladbar); die Liste kann nur ergänzen, nicht freischalten |
| `ONSHAPE_DAILY_REQUEST_LIMIT` | `500` | Max. abgerechnete Onshape-Requests pro Tag (UTC), 0 = unbegrenzt |
| `ONSHAPE_WEEKLY_REQUEST_LIMIT` | `600` | dto. pro Kalenderwoche |
| `ONSHAPE_MONTHLY_REQUEST_LIMIT` | `1000` | dto. pro Monat |
| `ONSHAPE_YEARLY_REQUEST_LIMIT` | `2400` | dto. pro Jahr |
| `TOOL_CALLS_PER_MINUTE` | `30` | Rate-Limit pro Token |
| `MAX_CONCURRENT_CALLS` | `4` | Parallele Tool-Ausführungen |
| `ONSHAPE_BASE_URL` | `https://cad.onshape.com` | Nur `https://*.onshape.com` erlaubt |
| `ALLOWED_HOSTS` | leer | Optionale Host-Header-Prüfung, z. B. `pikes-onshape-mcp.fly.dev` |
| `ENABLE_LEGACY_SSE` | `false` | `/sse`-Endpunkt für ältere Clients; Produktion: `true` |
| `MAX_RESPONSE_CHARS` / `MAX_REQUEST_BYTES` | `60000` / `1000000` | Größenlimits |
| `IMAGE_LINKS` | `true` | Screenshots zusätzlich als Bild-Link (`/img/<zufall>.png`), damit ClickUp sie anzeigt |
| `IMAGE_LINK_TTL_HOURS` | `24` | Gültigkeit der Bild-Links (1–168 h), danach gelöscht |
| `PUBLIC_BASE_URL` | `https://<FLY_APP_NAME>.fly.dev` | Basis-URL für Bild-/Download-Links |
| `CACHE_MAX_MB` | `500` | Max. Größe des Export-/Render-Caches auf dem Volume (älteste fliegen zuerst) |
| `FILE_CACHE_HOURS` | `24` | Lebensdauer großer Dateien (STEP/PDF/Renderings) auf dem Volume |

**Budget richtig wählen:** Onshape erlaubt pro Jahr z. B. 5.000 API-Calls pro Nutzer (Professional) bzw. 10.000 (Enterprise) – als Firmen-Pool. Faustregel: Monatslimit ≈ (Jahrespool − andere API-Nutzung) ÷ 12. Ein Tool-Aufruf braucht meist 1–3 Requests, Interferenz-Checks mehr. Aktueller Stand: Tool `get_server_policy`.

Die Rechte-Schalter (`WRITE_MODE`, `ALLOW_DELETE_FEATURE`, `ALLOW_FEATURESCRIPT`, `ENABLE_LEGACY_SSE`) stehen im Code standardmäßig auf aus; `fly.toml` schaltet bewusst frei, was Produktion braucht. Rechte ändern: Wert in `fly.toml` anpassen + `fly deploy` (z. B. `WRITE_MODE = "off"` als Notbremse → nur noch lesen).

## Betrieb

- **Logs/Audit:** `fly logs` – jede Tool-Ausführung, Admin-Aktion (`/admin/*`) und jeder Hintergrundjob als eine JSON-Zeile `AUDIT {...}`: `event` (`tool_call`/`admin`/`job`), `client`, `action`, `cls`, `decision`, `document`, `onshapeCalls` (abgerechnete Calls), `attempts` (gesendete Requests inkl. Fehlern), `blocked` (vom Guard gestoppt), `ms`, je nach Fall `reason`, `error`, `onshapeStatus`, `upstream_error`. Keine Secrets, keine CAD-Inhalte. Seit 2026-10-15 (H-6) heißen die Felder so (vorher `tool`, `onshape_requests`, `blocked_requests`); `/admin/webhook` meldet `onshapeCalls` statt `onshapeRequests` – Log-Filter und Admin-Skripte entsprechend anpassen.
- **Datenverzeichnis:** Ist das Verzeichnis von `USAGE_FILE` (`/data`) nicht beschreibbar oder lässt sich die Mapping-DB bzw. ein Cache nicht öffnen, startet der Server nicht (Exit 2, eine Zeile `Configuration error: …`). Der `/admin/sync`-Cooldown liegt in `mappings.db` und übersteht Neustarts.
- **Token rotieren:** neues Token als zweites Label ergänzen (`clickup:NEU,clickup-alt:ALT`), in ClickUp umstellen, altes entfernen.
- **Notaus:** `fly scale count 0` oder API-Key in Onshape löschen.
- **Upstream-Update:** neuen Stand von hedless/onshape-mcp prüfen, nach `src/onshape_mcp/` kopieren, `pytest` ausführen. Neue Tools bleiben gesperrt, bis sie in `src/onshape_secure/policy.py` (`TOOL_CLASSES`) eingestuft sind – der Test `test_every_upstream_tool_is_classified` schlägt dann an.

## Lokal testen

```bash
python -m venv .venv && . .venv/bin/activate
pip install --require-hashes --no-deps -r requirements.txt
pip install pytest pytest-asyncio
pytest                                   # 130 Tests (Policy, Auth, Exploit-Regressionen, E2E mit echtem MCP-Client)
PYTHONPATH=src pytest upstream_tests -o addopts="" -o asyncio_mode=auto   # 544 Upstream-Tests
cp .env.example .env   # ausfüllen
PYTHONPATH=src python -m onshape_secure  # http://localhost:8080/mcp
```

## Bekannte Grenzen

- Ein gemeinsamer Onshape-Account: Onshape-Historie zeigt Änderungen als „Service-Account“; wer in ClickUp was ausgelöst hat, steht im Audit-Log (pro Token-Label, nicht pro ClickUp-Person).
- Prompt-Injection: Text in Onshape-Dokumenten (Namen, Beschreibungen) landet im Kontext der KI. Die Policy verhindert trotzdem Schreiben außerhalb freigegebener Dokumente.
- Das Budget zählt nur Requests dieses Servers, nicht andere Onshape-API-Nutzung der Firma.
- `READ_ALLOWED_DOCUMENT_IDS` begrenzt die direkt abfragbaren Dokumente; Baugruppen zeigen aber auch Teile aus verlinkten Dokumenten (Screenshot, Positionen). Das echte Limit ist, was mit dem Service-Account geteilt ist.
- `get_face_coordinate_system` funktioniert nur in beschreibbaren Dokumenten (es braucht einen temporären Mate Connector).
