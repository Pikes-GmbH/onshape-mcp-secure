---
title: 'H-1: Restriktive Code-Defaults für sicherheitsrelevante Konfiguration'
type: 'chore'
created: '2026-10-15'
status: 'done'
route: 'oneshot'
review_loop_iteration: 0
context:
  - '{project-root}/_bmad-output/specs/spec-restrictive-config-defaults/SPEC.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** `config.py` hat offene Defaults (`WRITE_MODE=allowlist`, `ALLOW_DELETE_FEATURE`/`ALLOW_FEATURESCRIPT`/`ENABLE_LEGACY_SSE`=true); nur `fly.toml` hält Produktion read-only. Ein gesetztes `DISABLED_TOOLS` ersetzt die Export-Sperre still (Spine H-1, AD-10, AD-5).

**Approach:** Defaults auf `off`/`false`; `DISABLED_TOOLS` wird mit `DEFAULT_DISABLED_TOOLS` vereinigt; `fly.toml` setzt `ALLOW_FEATURESCRIPT=true`, `ENABLE_LEGACY_SSE=true`, `ALLOW_DELETE_FEATURE=false` explizit, sodass Produktion unverändert bleibt. Unit-Tests je Default, Doku (README, DOKUMENTATION.md, .env.example) angepasst. Kein Commit, kein Deploy ohne Erics OK.

</frozen-after-approval>

## Implementation Notes

- `config.py`: Defaults `WRITE_MODE=off` (auch bei leerem Wert), `ALLOW_DELETE_FEATURE`/`ALLOW_FEATURESCRIPT`/`ENABLE_LEGACY_SSE=false`; `DISABLED_TOOLS` wird mit `DEFAULT_DISABLED_TOOLS` vereinigt.
- `fly.toml`: explizit `ALLOW_FEATURESCRIPT=true`, `ENABLE_LEGACY_SSE=true`, `ALLOW_DELETE_FEATURE=false` (wirkungslos bei `WRITE_MODE=off`).
- `app.py`: Startlog zeigt jetzt `allow_delete`, `featurescript`, `legacy_sse`, `disabled_tools` (Review-Fund).
- Tests: Default je Variable, Vereinigung, `fly.toml`-Werte ergeben dieselbe Tool-Liste wie vorher, e2e `/sse` nicht geroutet bei aus; `_load` isoliert alle von `load_settings` gelesenen Variablen.
- Doku: README, DOKUMENTATION.md, `.env.example`.
- Lokal: Requirements sind Linux-gelockt; für Windows-venv zusätzlich `win32-setctime`, `colorama`, `pywin32` nötig (nicht in requirements.txt).
- Kein Commit, kein Deploy (Erics OK ausstehend).

## Review Triage Log

- `DISABLED_TOOLS`-Tippfehler unbemerkt — medium, vorbestehend → defer (deferred-work.md).
- Export-Freischaltung per `DISABLED_TOOLS=""` entfällt still — medium → patch: Hinweis in DOKUMENTATION.md; Produktion setzt die Variable nicht (Annahme in SPEC).
- Startlog zeigt Opt-in-Flags nicht — medium → patch (`app.py`).
- Kein e2e-Test für `/sse` aus — medium → patch (`test_legacy_sse_not_routed_when_off`).
- `.env.example` ohne neue Flags — low → patch.
- `_load` isoliert Umgebung nur teilweise — low → patch (alle gelesenen Variablen).
- Zeilenumbruch im Test verloren — low → patch.
- Testname/Kommentar zu `fly.toml` überbehauptet — low → patch (Kommentar präzisiert).
- Testanzahl in DOKUMENTATION veraltet — low → patch.
- `tomllib` braucht Python ≥ 3.11 — false: Projekt läuft auf 3.12 (Dockerfile, DOKUMENTATION).

### bmad-code-review (4 Layer, 2026-10-15)

- Formulierung „alle sicherheitsrelevanten Variablen“ zu breit (Prefix `AI-`, `IMAGE_LINKS`, `ALLOWED_HOSTS`) — medium → patch: Wortlaut auf die vier Rechte-Schalter eingeengt; Defaults selbst → defer.
- `DISABLED_TOOLS` ungeprüfte Namen — bereits deferred.
- `CONFIG_ENV`-Regex kann still Variablen verfehlen — low → patch (Guard-Assert).
- CAP-2-Test prüft nicht exakte Menge — low → patch (`==`).
- Leere Werte nur für `WRITE_MODE` getestet — low → patch (parametrisiert für alle drei Bools).
- `.env.example` ohne `DISABLED_TOOLS`/mehrdeutiger Kommentar — low → patch (Kommentare auf eigenen Zeilen, Inline-Kommentare brechen `--env-file`).
- conftest-Fixture permissiv ohne Erklärung — low → patch (Kommentar).
- Doku nennt interne ID „H-1“ und geschätzte Testzahl — low → patch (156).
- Baseline im `fly.toml`-Test handgebaut — low, im Kommentar offengelegt → reject.
- Leeres `WRITE_MODE` jetzt `off` statt `ConfigError` — false: von CAP-1 so gewollt.
- Kein positiver SSE-Routing-Test mit Produktionsflags — false: `fly.toml`-Test belegt `enable_legacy_sse=True`, e2e `sse_client`-Test belegt Routing bei `True`.
- `ALLOW_DELETE_FEATURE` in `fly.toml` true→false — false als Defekt: laut Auftrag gewollt, wirkungslos bei `WRITE_MODE=off`; Eric im Bericht genannt.
- Kein Changelog/Versionssprung — reject: Projekt führt keinen Changelog.
- Startlog ohne Read-Scope/Image-Links — low, außerhalb Umfang → reject.
- Verification Gap: keine Lücken.
