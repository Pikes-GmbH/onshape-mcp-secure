---
review-of: ARCHITECTURE-SPINE.md (onshape-mcp-secure, 2026-10-15)
lens: Adversary – zwei regelkonforme Einheiten, die trotzdem inkompatibel bauen
date: '2026-10-15'
status: final
---

# Adversary-Review: Architecture Spine onshape-mcp-secure

## Urteil

Der Spine schützt die **Außengrenze** gut (Auth, Default-deny, Write-Guard, Budget-Zähler). Er sagt aber fast nichts über die **Innenverträge** zwischen Features: welcher Speicher welche Datenklasse mit welcher Lebensdauer hält, wer eine Tabellenzeile schreiben darf, wie Kosten gemeldet werden, wie Admin-Endpunkte und Hintergrundjobs aussehen. Genau dort können zwei Agents, die jedes AD wörtlich befolgen, inkompatibel bauen. 8 Löcher gefunden, davon 3 hoch.

Alle Szenarien sind gegen den aktuellen Code (`src/onshape_secure/`, Stand `0011868`) geprüft. Einige Löcher existieren schon heute im Code; das ist markiert.

---

## L1 — „Unbefristet“ hat keinen Speicher (Caching-Semantik) · **HOCH**

**AD-Lücke:** AD-4 verlangt, Abgeleitetes aus Versionen „unbefristet“ wiederzuverwenden. AD-6 erlaubt im KV „Inhalte einer festen Version“. Kein AD sagt, *welcher* Speicher „unbefristet“ umsetzt und mit welcher Verdrängung.

**Realität im Code:**
- `FileCache` löscht nach `FILE_CACHE_HOURS=24` (`fly.toml`, max. 720 h) und bei `CACHE_MAX_MB` (`cache.py` `get`/`_evict`). Die Docstrings in `extra_tools.py` („cached forever“) sind falsch.
- `KVCache.set(key, v)` ohne TTL ist wirklich unbefristet; `_save` verdrängt bei >5000 Einträgen **die mit dem frühesten Ablauf zuerst** – unbefristete Einträge (`exp=None` → `inf`) überleben immer.

**Szenario:**
- *Feature A* „BOM eines freigegebenen Assemblies als CSV“ legt den BOM-JSON pro `versionId` unbefristet im KV ab (AD-6: „Inhalte einer festen Version“ ✔, AD-4 ✔).
- *Feature B* „STEP-Export“ (bestehend) legt das Ergebnis in den FileCache (24 h) und den laufenden Job als `pending:<key>` mit `PENDING_TTL=1800` im KV ab.

Nach einigen tausend BOMs füllt A das KV. Die Verdrängung wirft zuerst die kurzlebigen Einträge raus: `pending:*` (laufende Translations), `wu_pn_miss:*`. Folge: ein zweiter Aufruf von `export_released_model` findet den Job nicht mehr und **startet eine neue Translation** (POST + Polls + Download = 3–9 Calls doppelt). Gleichzeitig zahlt B nach 24 h den kompletten Export erneut, obwohl die Version unveränderlich ist. Beide halten sich an AD-4 – und widersprechen sich in der Lebensdauer derselben Datenklasse.

**Vorgeschlagene Regel (neues AD „Cache-Tiers“):**
> Jede gespeicherte Datenklasse gehört genau einem Tier. Die Tabelle im Spine ist abschließend:
>
> | Tier | Speicher | Schlüssel | Lebensdauer | Verdrängung |
> | --- | --- | --- | --- | --- |
> | `immutable` | `FileCache` (eigenes Verzeichnis, keine Zeit-TTL) | nur unveränderliche IDs (s. L4) | unbefristet | nur LRU über Größenlimit |
> | `fachlich` | `mappings.db` | Teilenummer/Revision | `_latest_ttl()` / `_ws_ttl()` + Webhook | keine |
> | `job` | KV mit TTL ≤ 1 h | Job-Schlüssel | TTL | **nie** durch andere Tiers verdrängbar |
> | `ephemeral` | `files/`, `links/`, `images/` | Zufallstoken | `IMAGE_LINK_TTL_HOURS` | Zeit |
>
> KV-Einträge ohne TTL sind nur für eine geschlossene Liste von Singletons erlaubt (`company_id`). Unbegrenzt wachsende Schlüsselräume im KV sind verboten. „Unbefristet“ in Tool-Beschreibungen nur, wenn das Tier `immutable` ist.

---

## L2 — Zwei Schreiber für dieselbe Zeile `latest[part_number, '']` · **HOCH** (existiert heute)

**AD-Lücke:** AD-6 macht `mappings.db` zum alleinigen *Speicherort*. Es sagt nicht, welche *Funktion* eine Tabelle besitzt und ihre Invarianten durchsetzt.

**Realität im Code (`mapping.py`):**
- `put_synced_revision` (Sync) schreibt `latest[pn,'']` mit der Regel „Teil/Baugruppe schlägt Zeichnung gleicher Nummer“.
- `put_revision` (On-Demand-Lookup ohne `element_type`) schreibt `latest[pn,'']` mit **dem, was Onshape gerade liefert** – auch eine Zeichnung (`elementType=2`).

**Szenario:**
- *Feature A* (Sync, bestehend) setzt `latest[PRT-1,''] → Teil Rev B`.
- *Feature B* „Zeichnungsnummer prüfen“ ruft `_revision("PRT-1", None)` nach Ablauf des TTL; Onshape gibt die Zeichnung zurück → `put_revision` überschreibt den Zeiger mit `etype=2`.

Danach liefert `find_part_by_number(PRT-1)` mal das Teil, mal die Zeichnung – je nachdem, wer zuletzt schrieb. `_model_revision` fängt das mit einem Zusatz-Call ab (Budget), `find_part_by_number` nicht. `released_parts()` joint auf `type_key = element_type` und sieht den `''`-Zeiger gar nicht. Beide Features erfüllen AD-6.

**Vorgeschlagene Regel (AD-6 schärfen):**
> Jede Tabelle in `mappings.db` hat genau eine Schreibfunktion pro Zeilenart; die Auflösungsregel für abgeleitete Zeilen (z. B. `latest[pn,'']` = Teil/Baugruppe vor Zeichnung) steht **einmal** in `mapping.py` und wird von allen Schreibern benutzt. Feature-Code ruft nie `INSERT` auf Tabellen anderer Features. Ein Test prüft: Sync und On-Demand-Lookup erzeugen für dieselbe Onshape-Antwort identische Zeilen.

---

## L3 — Zwei Wahrheiten für „ist freigegeben / welche Revision“ · **HOCH**

**AD-Lücke:** AD-6 zählt Revisionen *und* den Working-State-Index zu den Teile-Daten in `mappings.db` – aber `ws_parts` hat eigene Spalten `state` und `revision`. Welche Spalte autoritativ ist, sagt kein AD.

**Realität im Code:**
- `ws_parts.state/revision` stammt aus `/api/parts/d/{did}/w/{wid}` (Arbeitsstand).
- `revisions`/`latest` stammt aus Release-Management.
- `webhooks.py`: `onshape.revision.created` ruft nur `on_revision_created` (löscht `latest`, `drawings`), markiert aber **nicht** `ws_docs.dirty`. Mit aktivem Webhook bleibt der Index bis zu 30 Tage (`SAFETY_TTL`) alt, sofern nicht zufällig auch ein Metadaten-Event kommt.

**Szenario:**
- *Feature A* „Liste nicht freigegebener Teile eines Dokuments“ liest `ws_parts WHERE state <> 'RELEASED'` (reine Mapping-DB, 0 Calls, AD-4 ✔, AD-6 ✔).
- *Feature B* `export_mapping_table` (bestehend) entscheidet über „freigegeben“ anhand `revisions`.

Nach einer Freigabe zeigt A das Teil 30 Tage lang als „in Arbeit“, B als „freigegeben“. Beide AD-konform.

**Vorgeschlagene Regel (AD-6 ergänzen):**
> Freigabestatus und Revision sind **nur** aus `revisions`/`latest` abzuleiten. `ws_parts.state/revision` sind Anzeige-Felder und dürfen keine Entscheidung treffen. Jedes Release-Webhook-Event markiert zusätzlich das betroffene Dokument (`documentId` aus dem Payload, sonst alle) als `dirty`.

---

## L4 — Unveränderlicher Cache mit veränderlichem Schlüssel · **MITTEL-HOCH**

**AD-Lücke:** AD-4 erlaubt unbefristete Wiederverwendung für „Abgeleitetes aus Versionen“, regelt aber nicht den **Schlüssel**. AD-7 invalidiert nur `mappings.db`; KV und FileCache sieht der Webhook nie.

**Realität im Code:** Exporte und Screenshots sind korrekt auf `did:vid:eid:pid:config` geschlüsselt. Aber `wu_pn_miss:<partNumber>` und `rev2:<partNumber>|…` (KV-Fallback) sind schon heute teilenummer-geschlüsselt im KV und für den Webhook unsichtbar.

**Szenario:**
- *Feature A* „Zeichnungs-PDF zur aktuellen Revision“ cached das PDF unter `drawing-pdf:<partNumber>` – inhaltlich ist es ja aus einer Version abgeleitet (AD-4 ✔), es ist kein Teile-Datum, sondern Datei (AD-6 ✔).
- *Feature B* (Webhook) invalidiert nach einer neuen Zeichnungsfreigabe `drawings` und `latest`.

A liefert danach weiter die alte Revision aus – unbefristet.

**Vorgeschlagene Regel:**
> Ein Cache-Schlüssel im Tier `immutable` besteht ausschließlich aus unveränderlichen Onshape-IDs (`documentId`, `versionId`/`microversionId`, `elementId`, `partId`, `configuration`) plus Ausgabeparametern. Alles, was über eine Teilenummer, „latest“, einen Namen oder einen Workspace adressiert wird, ist Tier `fachlich` und lebt in `mappings.db` mit Webhook-Invalidierung. Auflösung „Teilenummer → Version“ immer zuerst über `mappings.db`, dann Cache-Treffer per Versions-Schlüssel.

---

## L5 — Uneinheitliche Kostenmeldung und Kontexte für Nicht-Tool-Aufrufer · **MITTEL-HOCH**

**AD-Lücke:** AD-3 bindet „alle Tools, Admin-Endpunkte, Hintergrundjobs“, beschreibt aber nur den Request-Pfad (`_metered`), nicht den **Aufrufkontext** (`_call_ctx`), aus dem Klasse, gegatetes Dokument, Audit und Kosten kommen.

**Realität im Code (`app.py`):**
- Zwei Zähler pro Kontext: `requests` (Versuche, inkl. erstatteter) und `billed` (abgerechnet). Die `[Onshape-API]`-Zeile nutzt `billed`, die `AUDIT`-Zeile `requests`.
- `admin_webhook` meldet `onshapeRequests` (= Versuche), `admin_sync` meldet `onshapeCalls` (= `billed`).
- Admin-Aktionen loggen `AUDIT admin_webhook method=…` als Freitext statt JSON (widerspricht der Audit-Konvention).
- Ein Hintergrundjob ohne `_call_ctx` (ctx `None`) bekommt `cls=None`: GETs gehen durch, Budget wird reserviert, aber es gibt **keine AUDIT-Zeile** und keine Kostensumme.

**Szenario:**
- *Feature A* „nächtlicher Auto-Sync“ als `asyncio`-Task im `lifespan`, ruft `extra.sync_revisions()` direkt (AD-3 ✔: läuft über den gemeterten Client; AD-8 ✔: Zustand im Prozess).
- *Feature B* `/admin/sync` (bestehend) mit Cooldown in `sync_state` – einer **lokalen Closure-Variable** von `admin_sync`.

A kennt den Cooldown nicht, läuft parallel zu B, beide lesen denselben Cursor `rev_sync_after` und holen dieselben Seiten doppelt. A taucht im Audit nicht auf; Eric sieht nur den Budgetverbrauch.

**Vorgeschlagene Regel (AD-3 erweitern):**
> Jeder Onshape-Aufrufer – Tool, Admin-Endpunkt, Hintergrundjob – läuft in genau einem `call_scope(actor, cls, doc)` aus `app.py`. Der Scope setzt `_call_ctx`, schreibt am Ende **eine** JSON-`AUDIT`-Zeile und meldet Kosten nur als `billed` (Feldname `onshapeCalls` überall). Batch-Jobs (Sync, Linking, Re-Index) haben einen gemeinsamen Job-Registry-Eintrag (Name → Lock + Cooldown); Admin-Endpunkt und Hintergrundjob desselben Jobs teilen ihn.

---

## L6 — Kein Kostendeckel pro Aufruf · **MITTEL**

**AD-Lücke:** AD-4 verbietet „Erkunden“, begrenzt aber nicht, wie viele Calls ein *einzelner* Tool-Aufruf auslösen darf. `budget.check()` prüft nur, ob vor dem Start noch Budget da ist.

**Realität im Code:** `_find_unreleased` indiziert bis zu `MAX_INDEX_DOCS_PER_LOOKUP=3` Dokumente plus Where-Used; ein Export kostet bis zu 9 Calls (`POLL_DELAYS_NEW`). Jeder Deckel ist eine lokale Konstante.

**Szenario:**
- *Feature A* „Where-used-Baum“ für ein Assembly: eine Where-Used-Abfrage pro Ebene, sauber per Batch-Endpunkt (AD-4 ✔), aber rekursiv bis 8 Ebenen → 40+ Calls pro Frage.
- *Feature B* hält sich an „≤ 3 Calls“. Ein ClickUp-Agent, der A fünfmal ruft, verbraucht einen Wochenanteil (600) spürbar. Beide AD-konform.

Zusätzlich fehlt Single-Flight: zwei parallele `find_part_by_number` für dieselbe unveröffentlichte Nummer indizieren dieselben Dokumente doppelt (`FileCache.lock` gibt es nur für Exporte).

**Vorgeschlagene Regel:**
> Jedes Tool deklariert bei der Registrierung (AD-11) `max_onshape_calls`; der Call-Scope bricht bei Überschreitung mit `QuotaExceeded` ab. Werte > 10 brauchen Erics OK. Jede Funktion, die einen gemeinsamen Speicher aus Onshape füllt (Index, Drawings, Exporte), läuft unter einem Single-Flight-Lock pro Ressourcenschlüssel.

---

## L7 — `read_restricted` hängt an einer Namensliste, nicht an der Klasse · **MITTEL** (existiert heute)

**AD-Lücke:** AD-2 kennt die Klassen `read`/`meta`, aber kein AD sagt, dass Daten aus `mappings.db` dokumentübergreifend sind und deshalb unter `READ_ALLOWED_DOCUMENT_IDS` gesperrt oder gefiltert werden müssen.

**Realität im Code (`policy.py` `is_tool_enabled`):** Unter `read_restricted` werden genau fünf Tools per **Name** abgeschaltet (`list_documents`, `search_documents`, `show_mapping_table`, `search_part_index`, `export_mapping_table`).

**Szenario:**
- *Feature A* „Freigaben der letzten Woche“ (liest `revisions`, Klasse `meta`, AD-2 ✔).
- *Feature B* `show_mapping_table` (bestehend, `meta`, auf der Namensliste).

Unter `read_restricted` ist B gesperrt, A nicht – A gibt Teilenummern und Dokumentnamen aus allen Dokumenten zurück, obwohl die Konfiguration genau das verhindern soll.

**Vorgeschlagene Regel (AD-2 ergänzen):**
> Neue Klasse `index` für Tools, die aus `mappings.db`/Caches dokumentübergreifend antworten. `is_tool_enabled` sperrt `index` unter `read_restricted` über die Klasse, nicht über Namen; alternativ filtert das Tool jede Zeile durch `check_read`. Ein Test listet alle Tools mit Mapping-Zugriff und prüft das.

---

## L8 — Registrierung: Kollisionen und Overrides · **NIEDRIG-MITTEL**

**AD-Lücke:** AD-11 verlangt ein Modul pro Fachbereich, das Schema, Klasse und Handler registriert – aber nicht, was bei Namenskollisionen passiert.

**Realität im Code (`app.py` `_published_tools`):** Erst Upstream-Tools, dann `EXTRA_TOOLS` in dasselbe Dict – der letzte gewinnt still. `get_parts` nutzt das bewusst als Override (steht in `EXTRA_TOOL_NAMES`, aber nicht in `TOOLS`). Dispatch läuft über `name in EXTRA_TOOL_NAMES`.

**Szenario:** `tools/drawings.py` und `tools/exports.py` registrieren beide `export_released_drawing` (z. B. nach der Aufteilung von `extra_tools.py`) oder überschreiben dasselbe Upstream-Tool unterschiedlich. Je nach Importreihenfolge gewinnt eins; Tests laufen grün, weil jedes Modul nur sich selbst testet.

**Vorgeschlagene Regel (AD-11 ergänzen):**
> Die Registry wirft beim Start bei doppeltem Tool-Namen (`ConfigError`, fail closed). Das Ersetzen eines Upstream-Tools ist nur mit explizitem `replaces="<name>"` erlaubt und wird getestet. Die Klasse kommt aus der Registrierung und wird nicht zusätzlich in `TOOL_CLASSES` gepflegt (eine Quelle).

---

## Weitere Beobachtungen (kein eigenes Loch)

- Admin-Endpunkte sind uneinheitlich: `/admin/webhook` läuft als `SYSTEM`, `/admin/sync` als `READ`; nur Sync hat Cooldown; Fehler kommen mal als 429, mal als 502. Mit L5 (einheitlicher Call-Scope + Antwort-Envelope `{ok, onshapeCalls, budget, error}`) erledigt.
- `company_id` hat zwei Heimaten (`meta` in `mappings.db`, Fallback im KV). Harmlos, aber unter L1 als zulässiges KV-Singleton benennen.
- Die `drawings`-Tabelle ist auf `(part_number, part_revision)` geschlüsselt, der Where-Used-Request aber auf `configuration`. Konfigurierte Teile mit gleicher Nummer würden sich eine Zeile teilen. Unter L2 klären (Schlüssel = Invariante der Tabelle).
- Upstream-Tools (`export_part_studio`, `capture_*_screenshot`) laufen an allen eigenen Caches vorbei. AD-4 sollte festhalten, dass eigene Tools die bevorzugten Pfade sind und Upstream-Duplikate mittelfristig gesperrt werden.

## Zusammenfassung der vorgeschlagenen Regeln

| # | Schwere | Regel | Ort im Spine |
| --- | --- | --- | --- |
| L1 | hoch | Cache-Tier-Tabelle (Speicher, Schlüssel, TTL, Verdrängung) abschließend; keine unbegrenzten KV-Schlüsselräume | neues AD-12 |
| L2 | hoch | Eine Schreibfunktion pro Tabelle/Zeilenart; Auflösungsregeln einmal in `mapping.py` | AD-6 |
| L3 | hoch | Freigabestatus nur aus `revisions`/`latest`; Release-Webhook markiert auch `ws_docs` dirty | AD-6, AD-7 |
| L4 | mittel-hoch | Immutable-Cache-Schlüssel nur aus unveränderlichen IDs | AD-4 / AD-12 |
| L5 | mittel-hoch | Ein `call_scope` für Tools/Admin/Jobs; Kosten nur `billed`; gemeinsame Job-Registry für Locks + Cooldowns | AD-3, AD-8 |
| L6 | mittel | `max_onshape_calls` pro Tool; Single-Flight pro Ressourcenschlüssel | AD-4, AD-11 |
| L7 | mittel | Klasse `index`, Sperre unter `read_restricted` über Klasse | AD-2 |
| L8 | niedrig-mittel | Doppelte Tool-Namen = Startfehler; Overrides nur explizit | AD-11 |
