- source_spec: `_bmad-output/implementation-artifacts/spec-h-1-restrictive-config-defaults.md`
  summary: Unbekannte Namen in `DISABLED_TOOLS` beim Start ablehnen (`ConfigError`), damit ein Tippfehler kein Tool offen lässt.
  evidence: `config.py` prüft die Namen nicht gegen `TOOL_CLASSES`; vorbestehend, nicht durch H-1 verursacht. Fix braucht Prüfung in `create_app` (config darf policy nicht importieren).
- source_spec: `_bmad-output/implementation-artifacts/spec-h-1-restrictive-config-defaults.md`
  summary: Weitere Defaults prüfen, die nicht am restriktivsten sind (`WRITE_ALLOWED_NAME_PREFIX=AI-`, `IMAGE_LINKS=true`, `ALLOWED_HOSTS` leer) – ggf. auf leer/false setzen und in `fly.toml` freischalten.
  evidence: Code-Review H-1 (Blind Hunter, Edge Case Hunter); außerhalb des H-1-Umfangs (Spine AD-10 nennt nur die vier Schalter). `AI-` wirkt nur bei `WRITE_MODE!=off`.
