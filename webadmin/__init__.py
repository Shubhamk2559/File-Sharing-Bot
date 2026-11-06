"""Web admin panel for HelpBot (served by the same FastAPI app at /admin).

Layout
  config.py    env settings (ADMIN_PANEL_*)
  security.py  password / TOTP / sessions / CSRF / login lockout / audit log
  core.py      request dispatcher, route table, structured errors (framework independent -> unit-testable)
  jobs.py      background job registry (progress + stop)
  models.py    Pydantic request models (every input is validated server-side)
  routes_*.py  the API, one file per feature area
  mount.py     the only FastAPI/Starlette-aware code: registers the endpoints on the app
  panel.html   the single-page UI (plain HTML/CSS/JS, no build step)

NOTE: no module in this package uses `from __future__ import annotations` (see tests/test_webadmin_wiring.py).
"""
