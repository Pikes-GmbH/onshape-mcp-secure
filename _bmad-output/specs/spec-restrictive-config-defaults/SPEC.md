---
id: SPEC-restrictive-config-defaults
companions:
  - ../../planning-artifacts/architecture/architecture-onshape-mcp-secure-2026-10-15/ARCHITECTURE-SPINE.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# H-1: Restriktive Code-Defaults für sicherheitsrelevante Konfiguration

## Why

Mandat aus der Architektur (AD-10, AD-5, Backlog H-1): Die Code-Defaults in `config.py` sind offen (`WRITE_MODE=allowlist`, `ALLOW_DELETE_FEATURE`/`ALLOW_FEATURESCRIPT`/`ENABLE_LEGACY_SSE` = true). Nur `fly.toml [env]` hält Produktion read-only. Fehlt eine Zeile oder läuft der Server anderswo (lokal, neue App), ist Schreibzugriff offen. Ein gesetztes `DISABLED_TOOLS` hebt zudem still die Export-Sperre auf. Eric hat entschieden: Schreibzugriff bleibt aus und wird nicht ausgebaut.

## Capabilities

- **CAP-1**
  - **intent:** Ohne gesetzte Variable (oder leer) startet der Server mit `WRITE_MODE=off`, `ALLOW_DELETE_FEATURE=false`, `ALLOW_FEATURESCRIPT=false`, `ENABLE_LEGACY_SSE=false`.
  - **success:** `load_settings()` mit nur Pflicht-Secrets liefert genau diese Werte (Unit-Test je Default).
- **CAP-2**
  - **intent:** `DISABLED_TOOLS` ergänzt die Default-Sperre (`export_part_studio`, `export_assembly`), statt sie zu ersetzen.
  - **success:** `DISABLED_TOOLS=foo` ergibt `{export_part_studio, export_assembly, foo}`; `DISABLED_TOOLS=""` ergibt die Default-Sperre.
- **CAP-3**
  - **intent:** `fly.toml` schaltet explizit frei, was Produktion heute nutzt.
  - **success:** Mit den `[env]`-Werten aus `fly.toml` geladen: `write_mode=off`, `allow_featurescript=True`, `enable_legacy_sse=True`; die veröffentlichte Tool-Liste ist identisch mit vorher (Test).
- **CAP-4**
  - **intent:** README, `docs/DOKUMENTATION.md` und `.env.example` zeigen die neuen Defaults und die Vereinigungs-Semantik von `DISABLED_TOOLS`.
  - **success:** Keine Doku-Stelle nennt mehr `allowlist`/`true` als Default dieser vier Variablen.

## Constraints

- Produktionsverhalten ändert sich nicht: gleiche Tools, `/sse` bleibt aktiv, Limits unverändert.
- `ALLOW_DELETE_FEATURE` in `fly.toml` auf `false`: wirkt nicht, solange `WRITE_MODE=off` (Klasse `delete` ist dann ohnehin gesperrt).
- Ungültige Werte beenden weiter den Start (`ConfigError`).
- Kein Edit in `src/onshape_mcp/`.

## Non-goals

- Kein Deploy, kein Commit ohne Erics OK.
- Keine Änderung an Schreibpfaden oder am HTTP-Guard.
- `IMAGE_LINKS`-Default und Abschaltung von Legacy-SSE bleiben außerhalb (eigene Punkte).
- Kein Weg, die Export-Tools per Env freizuschalten (bräuchte Code-Änderung).

## Success signal

- `python -m pytest -q tests` grün inkl. neuer Default-Tests; Diff von `fly.toml` zeigt nur explizite Opt-ins, `get_server_policy` liefert in Produktion nach Deploy dieselben Werte wie vorher.

## Assumptions

- `DISABLED_TOOLS` ist in Produktion nicht als Fly-Secret gesetzt (`fly.toml` nennt es nicht). Falls doch, blendet die Vereinigung zusätzlich die Export-Tools aus – gewollt.
