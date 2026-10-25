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
-------------------
It provisions nothing and connects to nothing. ``runtime_config`` reads
environment *names* only, and the report it returns carries presence booleans,
never values, so the startup log is safe to keep. No deployment, migration,
secret or billing resource is created by importing this module.
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI

import runtime_config


def build_application(environ: Optional[dict] = None) -> FastAPI:
    """Validate the runtime composition, then build the real app.

    ``environ`` is for tests: pass a mapping to evaluate one composition
    without mutating the process environment. Production calls this with the
    process environment (the default) at import time.
    """
    runtime_config.require(environ)
    from api.app import create_app

    return create_app()


# Imported by uvicorn/gunicorn. Raising here is the point: Cloud Run reports a
# failed revision instead of routing traffic to an unconfigured process.
application = build_application()
