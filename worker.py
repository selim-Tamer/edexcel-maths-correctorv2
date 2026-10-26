"""Cloud Run Jobs worker composition: discover -> claim -> engine -> settle.

One job per invocation (``--once`` - the Cloud Run Jobs task shape) or a
development loop (``--loop``). The worker executes each job through the API's
own reviewed handler (``app.state.run_marking_job``): the claim, the engine
call, the trusted registration path and the settle rules are the SAME code the
synchronous ``POST /v1/jobs/{job_id}/run`` endpoint uses. There is deliberately
no second implementation of the marking flow.

Identity and scope:

* Discovery is tenant-scoped and EXPLICIT. The worker is told which tenants it
  serves (``AI_MARKER_WORKER_TENANTS``, comma separated, or repeated
  ``--tenant``). It is never handed a privileged cross-tenant read, and it
  needs no new database grant - the runtime roles' zero-DELETE, append-only
  posture is untouched.
* Each job is executed as its own owner+tenant; the worker's own subject is
  what the request identity tuple carries.

Exit codes: ``0`` = nothing to do, or the job settled; ``1`` = composition
error (configuration, store); ``2`` = the job was claimed but could not be
settled cleanly.

Nothing here deploys, provisions or bills anything: this file is the prepared
worker entrypoint for the Cloud Run Jobs command in ``docs/BETA_DEPLOY_SETUP.md``,
which is still a prepared command, not an executed one.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import closing

import runtime_config
from api.app import ApiError, _job_api, create_app
from job_store import JobStoreError

SUBJECT = "system:marker-worker"
TENANTS_ENV = runtime_config.WORKER_TENANTS_ENV

EXIT_OK = 0
EXIT_COMPOSITION = 1
EXIT_JOB = 2

# Codes that mean the process is wrongly composed, not that the job failed.
COMPOSITION_CODES = ("model_unavailable", "dev_opt_in_required",
                     "store_unusable")


def tenants_from(environ: dict = None, cli_tenants=()) -> tuple:
    """The tenants this worker serves: CLI wins, else the env, de-duplicated.

    Empty is refused by ``main`` - a worker with no tenants has nothing to do
    and must not guess.
    """
    env = dict(os.environ if environ is None else environ)
    names = list(cli_tenants) or [
        part.strip() for part in (env.get(TENANTS_ENV) or "").split(",")]
    ordered = []
    for name in names:
        if name and name not in ordered:
            ordered.append(name)
    return tuple(ordered)


def run_one(app, api, connection, *, tenant_id: str, now: int = None) -> dict:
    """Discover and execute at most one job for one tenant.

    Returns a content-free outcome dict (ids, status, error code only): this
    is a log line, never student data.
    """
    candidate = api.find_runnable_job(connection, tenant_id=tenant_id, now=now)
    if candidate is None:
        return {"executed": False, "tenant_id": tenant_id}
    job_id = candidate["job_id"]
    owner = candidate["owner_subject"]
    try:
        payload = app.state.run_marking_job(
            job_id=job_id, who=(SUBJECT, owner, tenant_id))
    except ApiError as failure:
        # The shared handler settles what it claims; an error before or after
        # that is reported with its existing-vocabulary code, never invented.
        return {"executed": True, "tenant_id": tenant_id, "job_id": job_id,
                "status": "error", "error_code": failure.code}
    except JobStoreError as failure:
        return {"executed": True, "tenant_id": tenant_id, "job_id": job_id,
                "status": "error", "error_code": failure.detail}
    status = str(payload.get("status") or "")
    return {"executed": True, "tenant_id": tenant_id, "job_id": job_id,
            "status": status,
            "error_code": payload.get("error_code") or ""}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="worker", description="execute marking jobs (Cloud Run Jobs)")
    parser.add_argument("--once", action="store_true",
                        help="run at most one job and exit (the Jobs shape)")
    parser.add_argument("--loop", action="store_true",
                        help="keep looking for work (development)")
    parser.add_argument("--tenant", action="append", default=[],
                        help="tenant to serve; repeatable (overrides env)")
    parser.add_argument("--idle-seconds", type=float, default=2.0,
                        help="sleep between discoveries in --loop mode")
    args = parser.parse_args(argv)

    try:
        runtime_config.require()
    except runtime_config.RuntimeConfigError as failure:
        print(f"worker: runtime composition refused ({failure.code})",
              file=sys.stderr)
        return EXIT_COMPOSITION

    tenant_ids = tenants_from(cli_tenants=args.tenant)
    if not tenant_ids:
        print(f"worker: no tenants configured ({TENANTS_ENV} or --tenant)",
              file=sys.stderr)
        return EXIT_COMPOSITION

    api = _job_api()
    app = create_app()
    if args.once and args.loop:
        print("worker: choose --once or --loop, not both", file=sys.stderr)
        return EXIT_COMPOSITION

    mode_once = args.once or not args.loop  # default: one job, then exit
    while True:
        progressed = False
        for tenant_id in tenant_ids:
            try:
                with closing(api.require_connection()) as connection:
                    outcome = run_one(app, api, connection, tenant_id=tenant_id)
            except JobStoreError as failure:
                print(f"worker: job store unusable ({failure.detail})",
                      file=sys.stderr)
                return EXIT_COMPOSITION
            if outcome.get("executed"):
                print(f"worker: job {outcome.get('job_id')} "
                      f"{outcome.get('status')} "
                      f"({outcome.get('error_code') or 'ok'})")
                progressed = True
                code = str(outcome.get("error_code") or "")
                if code in COMPOSITION_CODES:
                    return EXIT_COMPOSITION
                if outcome.get("status") in ("failed", "error"):
                    return EXIT_JOB
                if mode_once:
                    return EXIT_OK
        if mode_once:
            return EXIT_OK
        if not progressed:
            time.sleep(max(0.0, float(args.idle_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
