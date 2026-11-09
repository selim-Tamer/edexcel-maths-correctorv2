"""Cloud Run ASGI entrypoint for the AI Maths Marker API.

WHY THIS FILE EXISTS
--------------------
``api/app.py`` builds the FastAPI app from injected trust boundaries and has no
module-level instance by design (tests choose their own fakes). The deployed
container needs one stable import target - ``uvicorn api.server:application`` -
that runs the runtime contract **first**: a revision with a missing database
DSN, OIDC audience, storage configuration or browser-origin allowlist must fail
to start, and therefore fail the revision, instead of serving a half-configured
API with fail-closed 503s that look like an outage.

WHAT IT DOES NOT DO
------------------
It provisions nothing. ``runtime_config`` reads environment *names* only, and
the report it returns carries presence booleans, never values, so the startup
log is safe to keep. In PRODUCTION it additionally opens one PostgreSQL
connection to verify the reviewed schema capability (BR-02 result context);
that connection is closed before the app is built. No deployment, migration,
secret or billing resource is created by importing this module.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

import result_service
import runtime_config


def build_application(environ: Optional[dict] = None) -> FastAPI:
    """Validate the runtime composition, then build the real app.

    ``environ`` is for tests: pass a mapping to evaluate one composition
    without mutating the process environment. Production calls this with the
    process environment (the default) at import time.

    BR-02A: a production process must additionally prove the PostgreSQL
    result/audit schema capability before it serves traffic. A missing DSN,
    unreachable server or absent v9 capability raises
    ``RuntimeConfigError('store_unusable')`` here - never an automatic
    development-store fallback.
    """
    report = runtime_config.require(environ)
    if report.get("production"):
        result_service.require_capability(environ)
    from api.app import create_app

    return create_app(production_mode=bool(report.get("production")))


# Imported by uvicorn/gunicorn. Raising here is the point: Cloud Run reports a
# failed revision instead of routing traffic to an unconfigured process.
application = build_application()
