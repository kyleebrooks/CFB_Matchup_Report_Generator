"""In-process job manager for asynchronous report generation.

State lives in memory, which is correct for this deployment: the systemd unit runs
Gunicorn with --workers 1 --threads 8, so every request hits the same process. If the
worker count is ever raised above 1, this needs to move to Redis or the database, and
the Rotowire scheduler would double-fire too.
"""

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import config
import pipeline

# Two reports may build at once; each one already fans out to ~10 threads of its own.
MAX_CONCURRENT_JOBS = 2
# How long a finished job stays queryable before it is swept.
JOB_TTL_SECONDS = 3600


def job_key(home_short: str, away_short: str) -> str:
    return f"{(home_short or '').strip()}|{(away_short or '').strip()}"


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="report"
        )

    # -- internals --------------------------------------------------------
    def _sweep(self):
        """Drop finished jobs past their TTL. Caller must hold the lock."""
        now = time.time()
        stale = [
            k for k, j in self._jobs.items()
            if j["state"] in ("done", "error") and now - (j.get("finished_at") or now) > JOB_TTL_SECONDS
        ]
        for k in stale:
            self._jobs.pop(k, None)

    def _set(self, key: str, **fields):
        with self._lock:
            job = self._jobs.get(key)
            if job:
                job.update(fields)

    # -- public API -------------------------------------------------------
    def submit(self, params: dict) -> dict:
        """Queue a report build. Returns the job snapshot.

        If a build for this matchup is already in flight, the existing job is returned
        instead of starting a duplicate — double-clicking Generate is harmless.
        """
        key = job_key(params["home_short"], params["away_short"])

        with self._lock:
            self._sweep()
            existing = self._jobs.get(key)
            if existing and existing["state"] in ("queued", "running"):
                return dict(existing)

            job = {
                "job_id": uuid.uuid4().hex[:12],
                "key": key,
                "state": "queued",
                "stage": "queued",
                "message": "Queued",
                "percent": 0,
                "home_short": params["home_short"],
                "away_short": params["away_short"],
                "home_full": params["home_full"],
                "away_full": params["away_full"],
                "created_at": time.time(),
                "finished_at": None,
                "result": None,
                "error": None,
                "detail": None,
            }
            self._jobs[key] = job
            snapshot = dict(job)

        self._pool.submit(self._run, key, params)
        return snapshot

    def _run(self, key: str, params: dict):
        def progress(stage, percent, message):
            self._set(key, state="running", stage=stage, percent=percent, message=message)

        self._set(key, state="running", stage="start", percent=1, message="Starting up")
        try:
            result = pipeline.generate(
                home_full=params["home_full"],
                away_full=params["away_full"],
                home_short=params["home_short"],
                away_short=params["away_short"],
                year=params.get("year"),
                kickoff=params.get("kickoff"),
                progress=progress,
            )
            self._set(
                key,
                state="done",
                stage="done",
                percent=100,
                message="Report ready",
                result=result,
                finished_at=time.time(),
            )
        except pipeline.PipelineError as e:
            logging.error(f"Report job {key} failed: {e.message} ({e.detail})")
            self._set(
                key,
                state="error",
                stage="error",
                percent=100,
                message=e.message,
                error=e.message,
                detail=(e.detail or "")[:500],
                finished_at=time.time(),
            )
        except Exception as e:
            logging.exception(f"Report job {key} crashed")
            self._set(
                key,
                state="error",
                stage="error",
                percent=100,
                message="Report generation failed",
                error="Report generation failed",
                detail=str(e)[:500],
                finished_at=time.time(),
            )

    def get(self, home_short: str, away_short: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_key(home_short, away_short))
            return dict(job) if job else None

    def snapshot_all(self) -> list[dict]:
        with self._lock:
            return [dict(j) for j in self._jobs.values()]


manager = JobManager()


def public_view(job: dict) -> dict:
    """Trim a job record down to what the frontend needs."""
    elapsed = int((job.get("finished_at") or time.time()) - job["created_at"])
    out = {
        "job_id": job["job_id"],
        "state": job["state"],
        "stage": job["stage"],
        "message": job["message"],
        "percent": job["percent"],
        "elapsed_seconds": elapsed,
        "home_team": job["home_short"],
        "away_team": job["away_short"],
    }
    if job["state"] == "done" and job.get("result"):
        out["result"] = job["result"]
    if job["state"] == "error":
        out["error"] = job.get("error")
        out["detail"] = job.get("detail")
    return out
