- source_spec: `_bmad-output/implementation-artifacts/spec-h-1-restrictive-config-defaults.md`
  summary: Unbekannte Namen in `DISABLED_TOOLS` beim Start ablehnen (`ConfigError`), damit ein Tippfehler kein Tool offen lässt.
  evidence: `config.py` prüft die Namen nicht gegen `TOOL_CLASSES`; vorbestehend, nicht durch H-1 verursacht. Fix braucht Prüfung in `create_app` (config darf policy nicht importieren).
- source_spec: `_bmad-output/implementation-artifacts/spec-h-1-restrictive-config-defaults.md`
  summary: Weitere Defaults prüfen, die nicht am restriktivsten sind (`WRITE_ALLOWED_NAME_PREFIX=AI-`, `IMAGE_LINKS=true`, `ALLOWED_HOSTS` leer) – ggf. auf leer/false setzen und in `fly.toml` freischalten.
  evidence: Code-Review H-1 (Blind Hunter, Edge Case Hunter); außerhalb des H-1-Umfangs (Spine AD-10 nennt nur die vier Schalter). `AI-` wirkt nur bei `WRITE_MODE!=off`.

- source_spec: `_bmad-output/implementation-artifacts/spec-h-4-h-8-webhook-trust-log-redaction.md`
  summary: Webhook-ID-Gate öffnet wieder, wenn `_verify_webhook_if_due` nach 404 `webhook_id` löscht und keine Metadaten-Webhook-ID gespeichert ist; dann werden Events beliebiger ID wieder verarbeitet.
  evidence: `app.py` `_verify_webhook_if_due` setzt `webhook_id=None`; `known` in `webhooks.py` wird leer. Folge nur Invalidierung bei kurzen TTLs (kein Vertrauensgewinn); Fix braucht persistente „je registriert“-Markierung in `app.py`, das spätere Pakete ändern.

- source_spec: `_bmad-output/implementation-artifacts/spec-h-4-h-8-webhook-trust-log-redaction.md`
  summary: Webhook-Route `/onshape/webhook/{secret}` hat kein Rate-Limit; mit geleaktem Geheimnis lassen sich wiederholt Invalidierungen/Re-Indexierungen auslösen.
  evidence: Vorbestehend; Schaden durch Tages-Budget-Cap begrenzt. Fix bräuchte Debounce/Rate-Limit pro Minute im Receiver.
