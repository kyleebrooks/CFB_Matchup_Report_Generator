"""In-process job manager for asynchronous report generation.

State lives in memory, which is correct for this deployment: the systemd unit runs
Gunicorn with --workers 1 --threads 8, so every request hits the same process. If the
worker count is ever raised above 1, this needs to move to Redis or the database, and
the Rotowire scheduler would double-fire too.

Jobs are addressable two ways: by job_id (the multi-tenant /v1 API) and by a
deduplication key (the legacy AFPLNA flow, keyed by matchup so a double-click on
Generate returns the running job instead of starting a second build).
"""

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import config
import pipeline

# Reports may build concurrently; each one already fans out to ~10 threads of its own.
MAX_CONCURRENT_JOBS = int(__import__('os').getenv('MAX_CONCURRENT_JOBS', '2'))
# How long a finished job stays queryable before it is swept.
JOB_TTL_SECONDS = 3600


def job_key(home_short: str, away_short: str) -> str:
    return f"{(home_short or '').strip()}|{(away_short or '').strip()}"


def _default_runner(params: dict, progress) -> dict:
    """Legacy matchup build."""
    return pipeline.generate(
        home_full=params["home_full"],
        away_full=params["away_full"],
        home_short=params["home_short"],
        away_short=params["away_short"],
        year=params.get("year"),
        kickoff=params.get("kickoff"),
        settings=params.get("settings"),
        watermark=params.get("watermark"),
        progress=progress,
    )


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}      # dedup key -> job
        self._by_id: dict[str, dict] = {}     # job_id    -> job
        self._pool = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="report"
        )

    # -- internals --------------------------------------------------------
    def _sweep(self):
        """Drop finished jobs past their TTL. Caller must hold the lock."""
        now = time.time()
        stale = [
            j for j in self._by_id.values()
            if j["state"] in ("done", "error")
            and now - (j.get("finished_at") or now) > JOB_TTL_SECONDS
        ]
        for job in stale:
            self._by_id.pop(job["job_id"], None)
            if job.get("key"):
                self._jobs.pop(job["key"], None)

    def _set(self, job_id: str, **fields):
        with self._lock:
            job = self._by_id.get(job_id)
            if job:
                job.update(fields)

    # -- public API -------------------------------------------------------
    def submit(
        self,
        params: dict,
        *,
        runner=None,
        key: str | None = None,
        meta: dict | None = None,
    ) -> dict:
        """Queue a build. Returns the job snapshot.

        When `key` is supplied and a job for that key is already in flight, the existing
        job is returned instead of starting a duplicate.
        """
        if key is None and "home_short" in params and "away_short" in params:
            key = job_key(params["home_short"], params["away_short"])

        with self._lock:
            self._sweep()
            existing = self._jobs.get(key) if key else None
            if existing and existing["state"] in ("queued", "running"):
                snapshot = dict(existing)
                # The caller needs to know no new build started, so it can account for
                # the request without double-counting the work.
                snapshot["deduplicated"] = True
                return snapshot

            job = {
                "job_id": uuid.uuid4().hex[:12],
                "key": key,
                "state": "queued",
                "stage": "queued",
                "message": "Queued",
                "percent": 0,
                "home_short": params.get("home_short"),
                "away_short": params.get("away_short"),
                "home_full": params.get("home_full"),
                "away_full": params.get("away_full"),
                "created_at": time.time(),
                "finished_at": None,
                "result": None,
                "error": None,
                "detail": None,
            }
            job.update(meta or {})
            self._by_id[job["job_id"]] = job
            if key:
                self._jobs[key] = job
            snapshot = dict(job)

        self._pool.submit(self._run, job["job_id"], params, runner or _default_runner)
        return snapshot

    def _run(self, job_id: str, params: dict, runner):
        def progress(stage, percent, message):
            self._set(job_id, state="running", stage=stage, percent=percent, message=message)

        self._set(job_id, state="running", stage="start", percent=1, message="Starting up")
        try:
            result = runner(params, progress)
            self._set(
                job_id,
                state="done", stage="done", percent=100,
                message="Report ready", result=result, finished_at=time.time(),
            )
        except pipeline.PipelineError as e:
            logging.error(f"Report job {job_id} failed: {e.message} ({e.detail})")
            self._set(
                job_id,
                state="error", stage="error", percent=100,
                message=e.message, error=e.message,
                detail=(e.detail or "")[:500], finished_at=time.time(),
            )
        except Exception as e:
            # An unexpected escape. Name the stage and the exception type — a bare
            # str(e) from deep in the stack is close to useless when it reaches the UI.
            stage = getattr(pipeline.generate, "current_stage", {}).get("label", "unknown stage")
            logging.exception(f"Report job {job_id} crashed during: {stage}")
            self._set(
                job_id,
                state="error", stage="error", percent=100,
                message=f"Report generation failed during: {stage}",
                error=f"Report generation failed during: {stage}",
                detail=f"{e.__class__.__name__}: {e}"[:500], finished_at=time.time(),
            )

    def get(self, home_short: str, away_short: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_key(home_short, away_short))
            return dict(job) if job else None

    def get_by_id(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._by_id.get(job_id)
            return dict(job) if job else None

    def for_account(self, account_id: int) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._by_id.values() if j.get("account_id") == account_id]

    def snapshot_all(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._by_id.values()]


manager = JobManager()


def public_view(job: dict) -> dict:
    """Trim a job record down to what a client needs."""
    elapsed = int((job.get("finished_at") or time.time()) - job["created_at"])
    out = {
        "job_id": job["job_id"],
        "state": job["state"],
        "stage": job["stage"],
        "message": job["message"],
        "percent": job["percent"],
        "elapsed_seconds": elapsed,
        "home_team": job.get("home_short"),
        "away_team": job.get("away_short"),
    }
    if job.get("report_type"):
        out["report_type"] = job["report_type"]
    if job.get("subject"):
        out["subject"] = job["subject"]
    if job["state"] == "done" and job.get("result"):
        out["result"] = job["result"]
    if job["state"] == "error":
        out["error"] = job.get("error")
        out["detail"] = job.get("detail")
    return out
