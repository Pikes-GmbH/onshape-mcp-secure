# Onshape-MCP-Server für ClickUp – Dokumentation

Stand: 15.10.2026 · Eric Krämer

## Überblick

Der Onshape-MCP-Server gibt ClickUp Brain und Super Agents Zugriff auf Onshape, abgesichert und mit möglichst wenigen Onshape-API-Aufrufen. Er läuft auf Fly.io in Frankfurt unter `https://pikes-onshape-mcp.fly.dev/mcp`.

- **Basis:** Open-Source-Projekt [hedless/onshape-mcp](https://github.com/hedless/onshape-mcp) (MIT-Lizenz), unverändert übernommen und um eine Sicherheitsschicht ergänzt.
- **Eigene Erweiterungen:** Teilenummer-Suche, Zeichnungs-Zuordnung, STEP- und PDF-Export, Zuordnungstabelle mit Cache, Kostenanzeige je Anfrage.
- **Onshape-Zugang:** ein API-Key mit Lese- und Schreibrecht (derzeit Erics Account). Alle ClickUp-Nutzer arbeiten über diesen einen Zugang.
- **Aktueller Modus:** nur lesen. Schreibende Tools sind abgeschaltet.
- **Code:** Projektordner `onshape-mcp-secure` (Python 3.12, Docker), 156 automatische Tests.

## Nutzung in ClickUp

Fragen stellt man in ClickUp Brain oder einem Super Agent in normaler Sprache. Bei Teilenummern geht die KI direkt zu den passenden Tools.

**Einbinden (Admin, nur im Browser):**

1. App Center → Custom MCP → Add New → Tab Workspace.
2. Name `Onshape`, URL `https://pikes-onshape-mcp.fly.dev/mcp`.
3. Authentifizierung „Autorisierungsheader“: Name `Authorization`, Wert `Bearer <Token>`. Das Token hat Eric.
4. Nach jedem Server-Update mit neuen Tools: Tool-Liste aktualisieren oder neu verbinden, bei Super Agents die Tools zusätzlich hinzufügen.

**Beispiel-Fragen:**

- „Zeig mir einen Screenshot von PRT-000593.“
- „Welche Zeichnung gehört zu PRT-000591?“
- „Exportiere das STEP von PRT-000593.“
- „Exportiere die Zeichnung zu PRT-000593 als PDF.“
- „Gib mir den Onshape-Link zu PRT-000585.“
- „Exportiere die Onshape-Zuordnungstabelle.“

**Wichtigste Tools:**

| Tool | Zweck | Onshape-Aufrufe |
| --- | --- | --- |
| `find_part_by_number` | Teil, Baugruppe oder Zeichnung per Nummer finden, mit Link; auch nicht freigegebene Teile | 0, sonst 1–3 beim ersten Mal |
| `find_drawing_for_part` | Zeichnungsnummer und Revision zu einem Teil | 0, sonst 1–2 |
| `capture_part_screenshot` | Bild genau eines Teils | 0–2 |
| `export_released_model` | STEP eines freigegebenen Teils als Download-Link | 0, sonst ~4 |
| `export_released_drawing` | Zeichnung als PDF, DXF oder DWG | 0, sonst ~4–6 |
| `search_part_index` | Teile im Index nach Name suchen | 0 |
| `show_mapping_table` / `export_mapping_table` | Zuordnungstabelle ansehen oder als CSV laden | 0 |
| `get_server_policy` | Regeln und Budget-Stand | 0 |

Dazu kommen die Lese-Tools des Originals (Dokumente, Part Studios, Baugruppen, Variablen). Sie lesen den Arbeitsstand, werden nicht gecacht und kosten jedes Mal.

**Ergebnisse in ClickUp:** Screenshots und Exporte kommen als Links, 24 Stunden gültig. Brain zeigt Bilder nur als Link; als Anhang an einer Aufgabe zeigt ClickUp sie direkt an.

## Sicherheit und Rechte

Ohne gültiges Token gibt der Server nichts heraus, und in Onshape schreiben kann derzeit niemand über ihn.

- **Zugang:** Jede Anfrage braucht `Authorization: Bearer <Token>` (alternativ `X-API-Key`). Frei erreichbar sind nur der Statuscheck, die 24-h-Links und der Onshape-Webhook mit geheimem Pfad.
- **Schreiben:** derzeit ganz aus (`WRITE_MODE = off`). Im Modus `allowlist` darf die KI nur in Dokumenten schreiben, deren Name mit `AI-` beginnt oder die per ID freigegeben sind. Neue Dokumente sind immer privat.
- **Zweite Sperre:** Unabhängig vom Tool blockt der Server jede schreibende Onshape-Anfrage, die nicht genau das freigegebene Dokument betrifft.
- **Links zu Dateien und Bildern:** zufällig (192 Bit), 24 Stunden gültig, danach gelöscht. Wer einen Link hat, kann ihn ohne Token öffnen.
- **Lesen:** Die KI sieht alles, was der Onshape-Account sieht. Wer weniger zeigen will, nimmt später einen eigenen Service-Account und teilt nur die nötigen Dokumente.

**Behobene Schwächen des Originals:**

| Problem im Original | Lösung |
| --- | --- |
| Neue Dokumente standardmäßig öffentlich | immer privat |
| Screenshots schreiben Dateien an beliebige Server-Pfade | Parameter entfernt |
| Feature-ID ungeprüft in der URL, Dokumente löschbar | strenge Prüfung aller IDs |
| API-Key bei Netzwerkfehlern im Log | Diagnose-Logging abgeschaltet |
| Lese-Tool `get_face_coordinate_system` schreibt temporär in Dokumente | als Schreib-Tool eingestuft |

Weitere Absicherung: Container ohne Root-Rechte, Abhängigkeiten per Hash gepinnt, Rate-Limit pro Token, Audit-Log jeder Tool-Ausführung. Eine unabhängige Sicherheitsprüfung hat sieben Befunde geliefert, alle sind behoben und mit Tests abgesichert.

- [ ] Den Onshape-API-Key erneuern: Er stand im Chat in einem curl-Befehl. Danach den neuen im Fly-Dashboard unter Secrets eintragen.

## Onshape-API-Budget

Pikes hat 2.500 Onshape-API-Aufrufe pro Jahr; der Server stoppt sich selbst, bevor dieses Kontingent aufgebraucht ist. Danach würde Onshape jeden Aufruf mit Fehler 402 ablehnen.

| Zeitraum | Limit des Servers |
| --- | --- |
| Tag (UTC) | 500 |
| Woche | 600 |
| Monat | 1.000 |
| Jahr | 2.400 |

- **Gezählt wird wie bei Onshape:** nur erfolgreiche Aufrufe. Fehlgeschlagene werden zurückgebucht; davon sind höchstens 1.000 pro Tag erlaubt.
- **Kostenzeile:** Jedes Ergebnis endet mit `[Onshape-API: n Aufruf(e) für diese Anfrage · heute … · Woche … · Monat … · Jahr …]`. Die KI nennt die Summe am Ende ihrer Antwort.
- **Budget-Warnung:** Ab 80 % des Monats- oder Jahreslimits trägt die Kostenzeile am Ende zusätzlich `· WARNUNG Onshape-Budget fast verbraucht: Monat …/… (… %)`; `get_server_policy` zeigt sie unter `budget_warning`. Die KI gibt sie an den Nutzer weiter. Einen eigenen Benachrichtigungskanal (Mail, ClickUp) gibt es nicht.
- **Kein Herumsuchen:** Die KI muss Teilenummern über `find_part_by_number` suchen. Findet sie nichts, fragt sie nach dem Dokument, statt Onshape zu durchstöbern. Früher kostete eine solche Suche 22 bis 30 Aufrufe.
- **Rate-Limit:** 30 Tool-Aufrufe pro Minute und Token, höchstens 4 parallel.

Den Stand zeigt jederzeit `get_server_policy` oder die Kostenzeile. Am 15.10.2026 lag das Jahr bei 219 Aufrufen, davon ein großer Teil für Aufbau und Tests.

## Zuordnungstabelle und Cache

Der Server führt eine eigene Tabelle aller Freigaben, Zeichnungen und indexierten Teile; die meisten Fragen beantwortet er daraus ohne Onshape-Aufruf. Sie liegt als SQLite-Datei auf dem verschlüsselten Fly-Volume und übersteht Neustarts.

**Inhalt (Stand 15.10.2026):**

| Bereich | Anzahl | Quelle |
| --- | --- | --- |
| Freigegebene Teile und Baugruppen | 286 | Sammelabruf aller Freigaben |
| Freigegebene Zeichnungen (DRW-…) | 143 | Sammelabruf aller Freigaben |
| Teil → Zeichnung verknüpft | 64 geprüft, 56 mit Zeichnung (Robot Gen3 komplett) | Where Used je Teil |
| Nicht freigegebene Teile im Index | 38 aus 3 Dokumenten | Teileliste je Dokument |

**Wie ein Teil gefunden wird:**

1. Zuordnungstabelle: freigegebene Teile und bekannte Indexeinträge, 0 Aufrufe.
2. Freigabe-Suche per Teilenummer, 1 Aufruf. Ist das Teil nicht freigegeben, ist der Fehlversuch kostenlos.
3. Where Used firmenweit per Teilenummer, 1 Aufruf. Liefert das Dokument, sofern das Teil in einer Baugruppe verbaut ist.
4. Dieses Dokument einmal komplett einlesen, 2 Aufrufe. Danach kostet jede Teilenummer daraus 0.
5. Nichts gefunden: Die KI fragt nach dem Dokument.

**Wie die Tabelle aktuell bleibt:**

- Onshape meldet neue Freigaben und geänderte Teilenummern per Webhook. Der Server löscht dann gezielt die betroffenen Einträge. Meldungen zählen nicht gegen das Budget.
- Feste Revisionen ändern sich nie und bleiben dauerhaft gespeichert.
- Alle Teile-Daten (Revisionen, „letzte Revision“, Zeichnungs-Links, Fehltreffer) stehen nur in dieser Tabelle. Gibt es zu einer Nummer ein Teil und eine Zeichnung, liefert die Suche ohne Typ das Teil, solange dessen Eintrag gültig ist (Sammelabruf und Einzelsuche folgen derselben Regel). Nach einer neuen Freigabe oder Ablauf der Gültigkeit kann sie bis zur nächsten Teile-Suche die Zeichnung liefern; die Antwort nennt dann den Typ `DRAWING`.
- Fehltreffer der firmenweiten Where-Used-Suche merkt sich der Server 1 Stunde.
- In Zuordnungstabelle und Teile-Index kommen Freigabestatus und Revision nur aus den Freigaben; nicht freigegebene Teile zeigen keinen Status und keine Revision aus dem Arbeitsstand. (`get_parts` liest den Arbeitsstand live und zeigt dessen Werte.)
- Einmal pro Woche prüft der Server mit 1 Aufruf, ob der Webhook noch lebt. Fällt er aus, wird stündlich neu geprüft.

**Große Dateien:** STEP, PDF und Bilder bleiben 24 Stunden ab Erstellung auf dem Volume, dann werden sie gelöscht. Die täglichen Fly-Snapshots (bleiben an, siehe Grenzen) halten sie bis zu 5 Tage.

**Tabelle exportieren:** `export_mapping_table` liefert die ganze Tabelle als CSV für Excel, ohne Onshape-Aufruf. Zellen, die mit `=`, `+`, `-` oder `@` beginnen, werden mit `'` entschärft, damit Excel keine Formeln aus Onshape-Namen ausführt.

## Betrieb und Administration

Der Server läuft als eine Fly.io-Maschine in Frankfurt (App `pikes-onshape-mcp`, Account von Eric). Im Leerlauf stoppt sie und startet beim nächsten Aufruf in wenigen Sekunden.

**Geheimnisse (Fly-Dashboard → App → Secrets):** `ONSHAPE_ACCESS_KEY`, `ONSHAPE_SECRET_KEY`, `MCP_AUTH_TOKENS`, `ADMIN_TOKEN`. Sie stehen nie im Code oder in `fly.toml`.

**Admin-Endpunkte (nur mit dem separaten Admin-Token `ADMIN_TOKEN`; das ClickUp-Token wird abgelehnt, `/admin/sync` höchstens alle 10 Minuten, auch über Neustarts hinweg):**

| Aufruf | Zweck | Onshape-Aufrufe |
| --- | --- | --- |
| `POST /admin/sync` | Alle neuen Freigaben in die Tabelle holen | 1 je 100 Freigaben |
| `POST /admin/sync?document=<Name>&maxParts=50` | Zusätzlich Teil → Zeichnung für ein Dokument | ~1 pro Teil |
| `POST /admin/webhook` | Onshape-Webhooks anmelden (einmalig) | 1 |
| `GET /admin/webhook` | Webhook- und Tabellenstatus | 0 |
| `GET /healthz` | Statuscheck, ohne Token | 0 |

**Häufige Handgriffe:**

- **Schreiben ein- oder ausschalten:** in `fly.toml` `WRITE_MODE` auf `off` oder `allowlist` setzen, dann neu deployen.
- **Token tauschen:** neues Token erzeugen (`openssl rand -hex 32`), in `MCP_AUTH_TOKENS` eintragen, in ClickUp neu verbinden.
- **Notaus:** `fly scale count 0` oder den API-Key in Onshape löschen.
- **Logs:** `fly logs`. Jede Tool-Ausführung, Admin-Aktion und jeder Hintergrundjob steht dort als JSON-`AUDIT`-Zeile mit Aktion, Dokument, Entscheidung und den abgerechneten Onshape-Aufrufen (`onshapeCalls`; `attempts` zählt auch fehlgeschlagene).
- **Startet nicht?** Ist `/data` nicht beschreibbar oder die Mapping-Tabelle kaputt, verweigert der Server den Start (`Configuration error` im Log), statt ohne Budgetzähler und Tabelle zu laufen.
- **Update des Originalprojekts:** neuen Stand prüfen, nach `src/onshape_mcp/` kopieren, Tests laufen lassen. Neue Tools bleiben gesperrt, bis sie in `policy.py` eingestuft sind.

**Kosten bei Fly.io:** eine kleine Maschine mit Auto-Stop und 1 GB Volume. Den genauen Betrag zeigt das Fly-Dashboard.

## Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen. Geheimnisse liegen als Fly-Secrets, alles andere in `fly.toml` (Änderung → neu deployen). Bei ungültigen Werten startet der Server nicht (fail closed). Die Rechte-Schalter (`WRITE_MODE`, `ALLOW_DELETE_FEATURE`, `ALLOW_FEATURESCRIPT`, `ENABLE_LEGACY_SSE`) stehen ohne Eintrag auf aus; `fly.toml` schaltet bewusst frei, was Produktion braucht.

| Variable | Aktuell / Standard | Bedeutung |
| --- | --- | --- |
| `ONSHAPE_ACCESS_KEY`, `ONSHAPE_SECRET_KEY` | Fly-Secret | API-Key des gemeinsamen Onshape-Accounts |
| `MCP_AUTH_TOKENS` | Fly-Secret | Bearer-Tokens der Clients, Format `label:token`, mind. 32 Zeichen, kommagetrennt |
| `ADMIN_TOKEN` | Fly-Secret | eigenes Token nur für die Admin-Endpunkte |
| `WRITE_MODE` | **off** (Standard: off) | `off` = nur lesen · `allowlist` = nur Dokumente mit Präfix oder aus der Liste · `all` = überall |
| `WRITE_ALLOWED_NAME_PREFIX` | `AI-` | Namenspräfix beschreibbarer Dokumente |
| `WRITE_ALLOWED_DOCUMENT_IDS` | leer | zusätzlich beschreibbare Dokumente |
| `READ_ALLOWED_DOCUMENT_IDS` | leer = alle | Lesen optional auf diese Dokumente beschränken; blendet Dokumentliste/-suche, Mapping-Tabelle und Teile-Index aus und lässt Treffer und Zeichnungen aus anderen Dokumenten weg |
| `ALLOW_DELETE_FEATURE` | false (Standard: false) | Features löschen; wirkt nur bei Schreibzugriff |
| `ALLOW_FEATURESCRIPT` | true (Standard: false) | FeatureScript auswerten (`eval_featurescript`, nur lesend) |
| `DISABLED_TOOLS` | leer | zusätzlich ausgeblendete Tools (unbekannte Namen verhindern den Start); `export_part_studio` und `export_assembly` sind immer gesperrt (auch `DISABLED_TOOLS=""` schaltet sie nicht frei) |
| `ONSHAPE_DAILY_REQUEST_LIMIT` | 500 | Onshape-Calls pro Tag (UTC) |
| `ONSHAPE_WEEKLY_REQUEST_LIMIT` | 600 | pro Woche |
| `ONSHAPE_MONTHLY_REQUEST_LIMIT` | 1000 | pro Monat |
| `ONSHAPE_YEARLY_REQUEST_LIMIT` | 2400 | pro Jahr (Puffer zum Kontingent von 2.500) |
| `TOOL_CALLS_PER_MINUTE` | 30 | Tool-Aufrufe pro Minute und Token |
| `MAX_CONCURRENT_CALLS` | 4 | gleichzeitige Tool-Aufrufe |
| `IMAGE_LINKS` | true | Screenshots als Link ausliefern |
| `IMAGE_LINK_TTL_HOURS` | 24 | Gültigkeit von Bild- und Download-Links |
| `FILE_CACHE_HOURS` | 24 | STEP, PDF und Renderings werden danach gelöscht |
| `CACHE_MAX_MB` | 500 | Obergrenze Datei-Cache |
| `PUBLIC_BASE_URL` | automatisch (Fly-App) | Basis-URL für Links |
| `USAGE_FILE` | `/data/usage.json` | Zählerstand des Budgets; sein Verzeichnis ist das Datenverzeichnis und muss beschreibbar sein |
| `ONSHAPE_BASE_URL` | cad.onshape.com | nur `*.onshape.com` zulässig |
| `ALLOWED_HOSTS` | leer | optionale Host-Header-Prüfung |
| `ENABLE_LEGACY_SSE` | true (Standard: false) | alter SSE-Transport zusätzlich zu `/mcp` |
| `MAX_RESPONSE_CHARS` / `MAX_REQUEST_BYTES` | 60.000 / 1 MB | Größenlimits |
| `LOG_LEVEL`, `PORT` | INFO, 8080 | Server |

## Kosten Fly.io

Abrechnung nur nach Nutzung, keine Grundgebühr.

| Posten | Preis | Bei uns |
| --- | --- | --- |
| Laufende Maschine (shared-cpu-1x, 512 MB, Frankfurt) | ca. 4,26 $/Monat bei Dauerbetrieb, sekundengenau | läuft dank Auto-Stop nur bei Anfragen → Cent-Beträge |
| Gestoppte Maschine (Root-Dateisystem) | 0,15 $/GB/Monat | wenige Cent |
| Volume 1 GB | 0,15 $/Monat | fällt immer an |
| Snapshots (täglich, 5 Tage) | erste 10 GB frei | 0 $ |
| Ausgehender Traffic | 0,02 $/GB | vernachlässigbar |
| Geteilte IPv4 / IPv6 | kostenlos | dedizierte IPv4 wäre 2 $/Monat |

Obergrenze knapp 4,50 $/Monat, selbst ohne Auto-Stop. Tatsächliche Kosten: Fly-Dashboard → Billing.

## Grenzen und Fehlerbehebung

### Grenzen

- **Ein gemeinsamer Onshape-Account:** Alle Aufrufe laufen über einen API-Key. Onshape sieht keine einzelnen Nutzer, der Server protokolliert nur das Token-Label.
- **Bilder in ClickUp Brain:** Screenshots erscheinen meist als Link, nicht eingebettet.
- **Neue Tools:** ClickUp zeigt sie erst nach dem Aktualisieren der MCP-Verbindung.
- **Unfreigegebene Teile**, die in keiner Baugruppe verbaut sind, findet der Server nur mit Dokument-Hinweis.
- **STEP- und Zeichnungsexport** nur für freigegebene Stände.
- **Where Used per Teilenummer** setzt Onshape Professional oder Enterprise voraus.
- **Snapshots:** Fly sichert das Volume täglich (5 Tage). Die Snapshots bleiben bewusst an: Sie sind das Backup von Zuordnungstabelle (`mappings.db`) und Budgetzähler (`usage.json`). Nach 24 h gelöschte Dateien können dort noch bis zu 5 Tage liegen.
- **Prompt Injection:** Texte aus Onshape (Namen, Beschreibungen) gehen ans Sprachmodell. Deshalb bleibt der Schreibzugriff aus.

### Fehlerbehebung

| Symptom | Ursache | Lösung |
| --- | --- | --- |
| ClickUp: „verbunden, aber nicht validiert“ | Header ohne „Bearer " | Wert als `Bearer <Token>` eintragen |
| „Budget erschöpft“ | Tages-, Wochen- oder Monatslimit erreicht | Reset abwarten (00:00 UTC) oder Limit in `fly.toml` anheben |
| Neue Tools fehlen in ClickUp | Tool-Liste zwischengespeichert | MCP-Verbindung in ClickUp aktualisieren |
| Neue Freigaben fehlen in der Tabelle | Webhook inaktiv | `/admin/webhook` aufrufen, Status mit `show_mapping_table` prüfen |
| Teil nicht gefunden | unfreigegeben und nirgends verbaut | Dokument nennen, der Server indexiert es dann |
| Link liefert 404 | 24 h abgelaufen | Tool erneut aufrufen |
| Erster Aufruf dauert länger | Maschine startet nach Auto-Stop | wenige Sekunden warten |
