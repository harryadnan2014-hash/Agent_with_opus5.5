"""Research runs in the background.

Streamlit reruns the whole page on every click, and a run that lives inside the
page's script dies with it - typing a question, switching page or pressing a button
used to cancel the research. So each run is a background thread owned by this
module rather than by any page:

* The page starts a job and only *watches* it (a fragment polls `progress()` once a
  second), so the user can keep clicking, chatting or browsing.
* The job saves the report into its project when it finishes, refunds the credits
  if it fails, and then answers any assistant questions asked while it was running.
* Jobs are registered by project id, process-wide, so a refreshed browser tab finds
  the same live run again.

Nothing here imports Streamlit.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .community import projects, quota
from .community.auth import Principal
from .config import RunSettings
from .pipeline import Progress

log = logging.getLogger("drugscope.jobs")


@dataclass
class ResearchJob:
    project_id: int
    settings: RunSettings
    started: float = field(default_factory=time.monotonic)
    events: list[Progress] = field(default_factory=list)
    # running -> answering (report saved, answering queued questions) -> finished
    phase: str = "running"
    error: str = ""
    thread: threading.Thread | None = None

    @property
    def latest(self) -> Progress | None:
        return self.events[-1] if self.events else None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


_JOBS: dict[int, ResearchJob] = {}
_lock = threading.Lock()


def active(project_id: int) -> ResearchJob | None:
    """The live job for a project, if one is running or still answering."""
    with _lock:
        return _JOBS.get(project_id)


def start(project_id: int, settings: RunSettings, principal: Principal, charge_id: int | None) -> ResearchJob:
    job = ResearchJob(project_id=project_id, settings=settings)

    def work() -> None:
        # Imported here so tests can replace `pipeline.run_sync`.
        from . import pipeline

        report = None
        try:
            for event in pipeline.run_sync(settings):
                job.events.append(event)
                if event.stage == "error":
                    job.error = f"{event.label}: {event.detail}"
                elif event.stage == "done":
                    report = event.payload
            if report is None:
                job.error = job.error or "The run ended without a report."
                projects.fail(project_id, job.error)
                if charge_id is not None:
                    quota.refund(principal, charge_id, reason=job.error or "run failed")
                return
            projects.finish(project_id, report)
            job.phase = "answering"
            # Keep going until no question is left: one asked while we answer is caught too.
            while projects.answer_pending(project_id, report, provider=settings.provider,
                                          gateway_model=settings.gateway_model):
                pass
        except Exception as exc:  # noqa: BLE001 - recorded on the project, never lost
            log.exception("research job %s failed", project_id)
            job.error = f"{type(exc).__name__}: {exc}"
            if report is None:
                projects.fail(project_id, job.error)
                if charge_id is not None:
                    quota.refund(principal, charge_id, reason=job.error or "run failed")
        finally:
            job.phase = "finished"
            with _lock:
                _JOBS.pop(project_id, None)

    job.thread = threading.Thread(target=work, name=f"research-{project_id}", daemon=True)
    with _lock:
        _JOBS[project_id] = job
    job.thread.start()
    return job


def wait(project_id: int, timeout: float = 60.0) -> None:
    """Block until a job finishes - for tests and scripts."""
    job = active(project_id)
    if job is not None and job.thread is not None:
        job.thread.join(timeout)


def summary(job: ResearchJob) -> dict[str, Any]:
    """What the page needs to draw progress."""
    latest = job.latest
    return {
        "phase": job.phase,
        "pct": latest.pct if latest else 0,
        "stage": latest.stage if latest else "plan",
        "label": latest.label if latest else "Starting research...",
        "elapsed": job.elapsed,
    }
