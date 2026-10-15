- source_spec: `_bmad-output/implementation-artifacts/spec-h-1-restrictive-config-defaults.md`
  summary: Weitere Defaults prüfen, die nicht am restriktivsten sind (`WRITE_ALLOWED_NAME_PREFIX=AI-`, `IMAGE_LINKS=true`, `ALLOWED_HOSTS` leer) – ggf. auf leer/false setzen und in `fly.toml` freischalten.
  evidence: Code-Review H-1 (Blind Hunter, Edge Case Hunter); außerhalb des H-1-Umfangs (Spine AD-10 nennt nur die vier Schalter). `AI-` wirkt nur bei `WRITE_MODE!=off`.

- source_spec: `_bmad-output/implementation-artifacts/spec-h-4-h-8-webhook-trust-log-redaction.md`
  summary: Webhook-ID-Gate öffnet wieder, wenn `_verify_webhook_if_due` nach 404 `webhook_id` löscht und keine Metadaten-Webhook-ID gespeichert ist; dann werden Events beliebiger ID wieder verarbeitet.
  evidence: `app.py` `_verify_webhook_if_due` setzt `webhook_id=None`; `known` in `webhooks.py` wird leer. Folge nur Invalidierung bei kurzen TTLs (kein Vertrauensgewinn); Fix braucht persistente „je registriert“-Markierung in `app.py`, das spätere Pakete ändern.

- source_spec: `_bmad-output/implementation-artifacts/spec-h-4-h-8-webhook-trust-log-redaction.md`
  summary: Webhook-Route `/onshape/webhook/{secret}` hat kein Rate-Limit; mit geleaktem Geheimnis lassen sich wiederholt Invalidierungen/Re-Indexierungen auslösen.
  evidence: Vorbestehend; Schaden durch Tages-Budget-Cap begrenzt. Fix bräuchte Debounce/Rate-Limit pro Minute im Receiver.

- source_spec: `_bmad-output/implementation-artifacts/spec-h-5-h-6-h-7-call-scope-data-dir.md`
  summary: Fehlt der Volume-Mount auf Fly, ist das im Docker-Image angelegte `/data` trotzdem beschreibbar; der Server startet dann mit flüchtigem Budgetzähler und leerer Mapping-DB.
  evidence: `Dockerfile` legt `/data` an und gibt es `mcp`; die H-7-Prüfung (Probe-Datei) erkennt nur „nicht beschreibbar“. Fix: Mount prüfen (z. B. `os.path.ismount` hinter einem Schalter wie `REQUIRE_DATA_MOUNT=true` in `fly.toml`) – braucht Erics OK für eine neue Variable.

- source_spec: `_bmad-output/implementation-artifacts/spec-h-5-h-6-h-7-call-scope-data-dir.md`
  summary: Ein kaputtes `usage.json` setzt den Budgetzähler beim Start still auf 0 (`OnshapeBudget._load` warnt nur), statt den Start zu verweigern.
  evidence: Vorbestehend in `quota.py`; atomisches Schreiben macht den Fall selten. Fail-closed hieße: manuelles Reparieren vor jedem Start – Betriebsentscheidung für Eric (Code-Review Edge Case Hunter).

- source_spec: `_bmad-output/implementation-artifacts/spec-h-5-h-6-h-7-call-scope-data-dir.md`
  summary: Volles Volume (ENOSPC) lässt die H-7-Probe scheitern; der Server startet nicht, und das Aufräumen, das Platz schaffen würde, läuft erst nach dem Start.
  evidence: `_check_data_dir` vor `_cleanup_files`; Datei-Cache ist auf 500 MB von 1 GB begrenzt, Bilder/Links nur per TTL – daher unwahrscheinlich. Fix: vor der Probe abgelaufene Dateien löschen oder ENOSPC gesondert melden (Code-Review Edge Case Hunter).

- source_spec: `_bmad-output/implementation-artifacts/spec-h-2-h-3-mapping-ownership.md`
  summary: Nach Release-Webhook (löscht alle `latest`-Zeilen der Nummer) oder abgelaufener TTL kann eine gleichnamige Zeichnung den untypisierten Zeiger übernehmen; `find_part_by_number` ohne Typ liefert dann die Zeichnung, bis eine typisierte Teile-Suche läuft.
  evidence: Vorbestehend (alter Sync und Lookup gleich); Teil-Vorrang über Invalidierung hinweg bräuchte einen Onshape-Call oder Rückgriff auf eine ungeprüfte Revision aus `revisions`. Code-Review H-2/H-3 (Blind Hunter, Edge Case Hunter).
