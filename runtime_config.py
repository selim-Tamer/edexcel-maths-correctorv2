"""Production runtime composition contract (env names + fail-closed validation).

WHAT THIS MODULE IS
-------------------
The single testable source of truth for the environment a deployed process
reads. ``evaluate()`` answers two questions without side effects:

* is this process claiming to be the production runtime?
* which components are missing or contradictory?

WHAT IT IS NOT
--------------
* It never returns, logs or fingerprints secret VALUES. The result carries
  presence booleans and problem codes only, so it is safe to print in a
  startup log or attach to a review artifact.
* It does not read values into module state and never connects anywhere;
  the Cloud Run entrypoint calls it once per process start.
* It provisions nothing. Deploying/migrating is a separate, explicitly
  approved operator step (see docs/BETA_DEPLOY_SETUP.md).

FAILURE VOCABULARY
------------------
Problem codes are drawn from the application's existing refusal vocabulary
(``store_unusable``, ``oidc_not_configured``, ``dev_opt_in_required``,
``model_unavailable``) - no new code is invented. A misconfigured process
therefore fails closed with the same class of error the endpoints already
emit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Optional

RUNTIME_ENV = "AI_MARKER_RUNTIME"
RUNTIME_PRODUCTION = "production"
RUNTIME_DEVELOPMENT = "development"

PG_DSN_ENV = "AI_MARKER_PG_DSN"
OIDC_AUDIENCE_ENV = "GOOGLE_OIDC_AUDIENCE"
SUPABASE_URL_ENV = "AI_MARKER_SUPABASE_URL"
SUPABASE_KEY_ENV = "AI_MARKER_SUPABASE_SERVICE_KEY"
BUCKET_ENV = "AI_MARKER_STORAGE_BUCKET"
ENGINE_REAL_ENV = "AI_MARKER_ENGINE_REAL"
SCHEME_ENV = "AI_MARKER_MARK_SCHEME"
GEMINI_KEY_ENV = "GEMINI_API_KEY"
PORT_ENV = "PORT"

# Development-only SQLite paths. Any of these being set in the production
# runtime means the process would serve development storage with production
# auth - refuse it instead of guessing.
DEV_STORE_ENVS = (
    "AI_MARKER_AUDIT_DEV_STORE",
    "AI_MARKER_DOCUMENT_DB",
    "AI_MARKER_JOB_DB",
    "AI_MARKER_INGEST_DB",
    "AI_MARKER_MEMBERSHIP_DB",
)


@dataclass(frozen=True)
class EnvVar:
    """One environment variable in the runtime contract."""

    name: str
    component: str
    secret: bool
    required_in_production: bool
    purpose: str


CONTRACT: tuple[EnvVar, ...] = (
    EnvVar(RUNTIME_ENV, "runtime", False, False,
           "'production' enables the strict gates; anything else is development"),
    EnvVar(PG_DSN_ENV, "database", True, True,
           "Supabase PostgreSQL pooler DSN (TLS required)"),
    EnvVar(OIDC_AUDIENCE_ENV, "identity", False, True,
           "Google OAuth Web client ID used as the expected OIDC audience"),
    EnvVar(SUPABASE_URL_ENV, "storage", False, True,
           "Supabase project HTTPS URL (never a secret)"),
    EnvVar(SUPABASE_KEY_ENV, "storage", True, True,
           "Supabase service-role key for the private PDF bucket (server only)"),
    EnvVar(BUCKET_ENV, "storage", False, True,
           "private Supabase Storage bucket name"),
    EnvVar(ENGINE_REAL_ENV, "engine", False, False,
           "'1' opts in to the paid marking engine; default off"),
    EnvVar(SCHEME_ENV, "engine", False, False,
           "path to the mark-scheme PDF inside the container"),
    EnvVar(GEMINI_KEY_ENV, "engine", True, False,
           "Gemini API key, only read when the engine is opted in"),
    EnvVar(PORT_ENV, "runtime", False, False,
           "Cloud Run injects the listen port (default 8080)"),
)

_SECRET_NAMES = frozenset(item.name for item in CONTRACT if item.secret)


class RuntimeConfigError(Exception):
    """Raised by ``require()`` when the runtime cannot start safely."""

    def __init__(self, code: str, problems: Iterable[dict]):
        self.code = code
        self.problems = list(problems)
        detail = "; ".join(f"{p['code']}: {p['detail']}"
                           for p in self.problems)
        super().__init__(detail or code)


def _present(env: dict, name: str) -> bool:
    return bool((env.get(name) or "").strip())


def _problem(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def _valid_dsn(value: str) -> bool:
    value = value.strip()
    if not (value.startswith("postgresql://") or value.startswith("postgres://")):
        return False
    # Supabase requires TLS; refuse a DSN that would downgrade to plaintext.
    return "sslmode=require" in value or "sslmode=verify-full" in value


def _valid_supabase_url(value: str) -> bool:
    value = value.strip().rstrip("/")
    return value.startswith("https://") and value.endswith(".supabase.co")


def evaluate(environ: Optional[dict] = None) -> dict:
    """Return a redacted composition report (never contains secret values).

    ``ready`` is only True when the claimed runtime has everything it needs
    and no development-only storage is configured in production.
    """
    env = dict(os.environ if environ is None else environ)
    runtime = (env.get(RUNTIME_ENV) or RUNTIME_DEVELOPMENT).strip().lower()
    problems: list[dict] = []
    if runtime not in (RUNTIME_DEVELOPMENT, RUNTIME_PRODUCTION):
        problems.append(_problem(
            "dev_opt_in_required",
            f"{RUNTIME_ENV} must be '{RUNTIME_DEVELOPMENT}' or "
            f"'{RUNTIME_PRODUCTION}'"))
        runtime = RUNTIME_DEVELOPMENT
    production = runtime == RUNTIME_PRODUCTION

    components: dict[str, dict] = {}
    for item in CONTRACT:
        components[item.name] = {
            "component": item.component,
            "configured": _present(env, item.name),
            "required_in_production": item.required_in_production,
            "secret": item.secret,
        }

    engine_opted_in = (env.get(ENGINE_REAL_ENV) or "").strip() == "1"
    components[ENGINE_REAL_ENV]["configured"] = engine_opted_in

    if not production:
        return {
            "runtime": runtime,
            "production": False,
            "ready": not problems,
            "problems": problems,
            "components": components,
            "engine": {"opted_in": engine_opted_in,
                       "configured": engine_opted_in
                       and _present(env, GEMINI_KEY_ENV)
                       and _present(env, SCHEME_ENV)},
            "mode_label": "development composition (fail-closed defaults; "
                          "development-only stores are opt-in)",
        }

    for name in DEV_STORE_ENVS:
        if _present(env, name):
            problems.append(_problem(
                "dev_opt_in_required",
                f"{name} is a development-only store path and must not be "
                f"set when {RUNTIME_ENV}={RUNTIME_PRODUCTION}"))

    dsn = (env.get(PG_DSN_ENV) or "").strip()
    if not dsn:
        problems.append(_problem("store_unusable",
                                 f"{PG_DSN_ENV} is required in production"))
    elif not _valid_dsn(dsn):
        problems.append(_problem(
            "store_unusable",
            f"{PG_DSN_ENV} must be a postgresql:// DSN with sslmode=require"))

    if not _present(env, OIDC_AUDIENCE_ENV):
        problems.append(_problem("oidc_not_configured",
                                 f"{OIDC_AUDIENCE_ENV} is required in production"))

    url = (env.get(SUPABASE_URL_ENV) or "").strip()
    if not url:
        problems.append(_problem("store_unusable",
                                 f"{SUPABASE_URL_ENV} is required in production"))
    elif not _valid_supabase_url(url):
        problems.append(_problem(
            "store_unusable",
            f"{SUPABASE_URL_ENV} must be an https://<ref>.supabase.co URL"))
    if not _present(env, SUPABASE_KEY_ENV):
        problems.append(_problem(
            "store_unusable",
            f"{SUPABASE_KEY_ENV} is required in production (server-side only)"))
    if not _present(env, BUCKET_ENV):
        problems.append(_problem("store_unusable",
                                 f"{BUCKET_ENV} is required in production"))

    if engine_opted_in:
        missing = [name for name in (GEMINI_KEY_ENV, SCHEME_ENV)
                   if not _present(env, name)]
        if missing:
            problems.append(_problem(
                "model_unavailable",
                f"{ENGINE_REAL_ENV}=1 but missing: {', '.join(missing)}"))

    return {
        "runtime": runtime,
        "production": True,
        "ready": not problems,
        "problems": problems,
        "components": components,
        "engine": {"opted_in": engine_opted_in,
                   "configured": engine_opted_in
                   and _present(env, GEMINI_KEY_ENV)
                   and _present(env, SCHEME_ENV)},
        "mode_label": "production composition (strict; development stores "
                      "refused)",
    }


def require(environ: Optional[dict] = None) -> dict:
    """Return the report, or raise ``RuntimeConfigError`` (fail closed)."""
    report = evaluate(environ)
    if not report["ready"]:
        raise RuntimeConfigError(report["problems"][0]["code"],
                                 report["problems"])
    return report


def secret_names() -> frozenset:
    """Names that must come from Secret Manager, never from plain env files."""
    return _SECRET_NAMES
