"""Background job registry: long tasks (bulk, copy chats, auto-post runs, mass delete) run as asyncio tasks;
the UI polls GET /admin/api/jobs/{id} for progress and can set the stop flag."""
import asyncio
import logging
import secrets
import time
from collections import deque

log = logging.getLogger("webadmin.jobs")


class Job:
    def __init__(self, kind: str, title: str, group: str):
        self.id = secrets.token_urlsafe(6)
        self.kind, self.title, self.group = kind, title, group
        self.status = "running"          # running | done | failed | stopped
        self.started = time.time()
        self.ended = None
        self.progress: dict = {}
        self.result: dict = {}
        self.error = ""
        self.logs: deque = deque(maxlen=200)
        self.ctl = {"stop": False}       # shared with the service layer (job["stop"])
        self.task = None

    def log(self, line: str) -> None:
        self.logs.append({"t": time.time(), "m": str(line)[:300]})

    def view(self, with_logs: bool = True) -> dict:
        d = {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status, "started": self.started,
             "ended": self.ended, "progress": self.progress, "result": self.result, "error": self.error,
             "stop_requested": self.ctl["stop"]}
        if with_logs:
            d["logs"] = list(self.logs)
        return d


class JobBusy(Exception):
    pass


class JobRegistry:
    def __init__(self, keep: int = 40):
        self._jobs: dict = {}
        self._order: deque = deque()
        self.keep = keep

    def running(self, group: str | None = None) -> list:
        return [j for j in self._jobs.values() if j.status == "running" and (group is None or j.group == group)]

    def start(self, kind: str, title: str, runner, group: str | None = None) -> Job:
        """runner: async def (job) -> dict(result). Only one running job per group."""
        group = group or kind
        if self.running(group):
            raise JobBusy(f"A '{group}' job is already running")
        job = Job(kind, title, group)
        self._jobs[job.id] = job
        self._order.append(job.id)
        while len(self._order) > self.keep:
            old = self._order.popleft()
            if self._jobs.get(old) and self._jobs[old].status != "running":
                self._jobs.pop(old, None)
            else:
                self._order.append(old)
                break
        job.task = asyncio.ensure_future(self._run(job, runner))
        return job

    async def _run(self, job: Job, runner) -> None:
        try:
            res = await runner(job)
            job.result = res or {}
            job.status = "stopped" if job.ctl["stop"] else "done"
        except asyncio.CancelledError:
            job.status = "stopped"
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job.kind)
            job.status, job.error = "failed", str(e)[:300]
            job.log(f"failed: {job.error}")
        finally:
            job.ended = time.time()

    def get(self, job_id: str):
        return self._jobs.get(job_id)

    def list(self) -> list:
        return [j.view(with_logs=False) for j in sorted(self._jobs.values(), key=lambda j: -j.started)]

    def stop(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if not j or j.status != "running":
            return False
        j.ctl["stop"] = True
        j.log("stop requested")
        return True
