"""Research projects - every run, saved with its report and its assistant chat.

A project is created the moment a run starts (status `running`), so it has a place -
its own page and link - while the research is still in progress. The background job
saves the report into it when the run finishes (`done`) or records why it did not
(`failed`). Reports are stored as JSON (`export.to_json`), never pickled, so reopening
a project can only ever read data.

Projects are private: every public function checks that the signed-in person owns
the project, so changing the number in a project link shows nothing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .. import export
from ..models import Report
from .auth import Principal, require_user
from .db import connect, now_iso

log = logging.getLogger("drugscope.projects")

MAX_QUESTION = 2000


class ProjectNotFound(Exception):
    pass


def _own(conn, user_id: int, project_id: int):
    row = conn.execute("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user_id)).fetchone()
    if row is None:
        raise ProjectNotFound("Project not found.")
    return row


def _meta(row) -> dict[str, Any]:
    data = {k: row[k] for k in row.keys() if k != "report_json"}
    data["summary"] = json.loads(row["summary"] or "{}")
    return data


# --------------------------------------------------------------------------- #
# Lifecycle (called by the research job)
# --------------------------------------------------------------------------- #

def create(principal: Principal, query: str, depth: str) -> int:
    user = require_user(principal)
    title = " ".join(query.split())[:140] or "Untitled research"
    stamp = now_iso()
    with connect() as conn:
        return int(conn.execute(
            "INSERT INTO projects (user_id, title, query, depth, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, title, query, depth, stamp, stamp),
        ).lastrowid)


def finish(project_id: int, report: Report) -> None:
    summary = {
        "sources": len(report.records),
        "quality": report.metrics.get("quality", {}).get("score"),
        "verdict": report.synthesis.summary.verdict[:300],
        "confidence": report.synthesis.summary.confidence,
        "elapsed": report.elapsed_seconds,
        "cost": round(report.usage.cost_usd, 4),
    }
    with connect() as conn:
        conn.execute(
            "UPDATE projects SET status = 'done', report_json = ?, summary = ?, error = '', updated_at = ? "
            "WHERE id = ?", (export.to_json(report), json.dumps(summary), now_iso(), project_id))


def fail(project_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE projects SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                     (error[:1000], now_iso(), project_id))


def mark_interrupted() -> int:
    """At startup: runs that were in flight when the app stopped can never finish."""
    with connect() as conn:
        return conn.execute(
            "UPDATE projects SET status = 'failed', error = 'Interrupted - the app was restarted while this "
            "research was running. Run it again from the project page.', updated_at = ? WHERE status = 'running'",
            (now_iso(),)).rowcount


# --------------------------------------------------------------------------- #
# Owner operations
# --------------------------------------------------------------------------- #

def list_projects(principal: Principal, search: str = "", limit: int = 100) -> list[dict[str, Any]]:
    user = require_user(principal)
    with connect() as conn:
        rows = conn.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM project_messages m WHERE m.project_id = p.id AND m.role = 'user') "
            "AS questions FROM projects p WHERE p.user_id = ? AND (? = '' OR lower(p.title) LIKE ?) "
            "ORDER BY p.updated_at DESC LIMIT ?",
            (user.id, search.strip(), f"%{search.strip().lower()}%", limit)).fetchall()
    out = []
    for row in rows:
        meta = _meta(row)
        meta["questions"] = row["questions"]
        out.append(meta)
    return out


def get(principal: Principal, project_id: int) -> dict[str, Any]:
    user = require_user(principal)
    with connect() as conn:
        return _meta(_own(conn, user.id, project_id))


def load_report(principal: Principal, project_id: int) -> Report | None:
    user = require_user(principal)
    with connect() as conn:
        row = _own(conn, user.id, project_id)
    return export.report_from_json(row["report_json"]) if row["report_json"] else None


def rename(principal: Principal, project_id: int, title: str) -> None:
    user = require_user(principal)
    title = " ".join((title or "").split())[:140]
    if not title:
        raise ValueError("Give the project a name.")
    with connect() as conn:
        _own(conn, user.id, project_id)
        conn.execute("UPDATE projects SET title = ? WHERE id = ?", (title, project_id))


def delete(principal: Principal, project_id: int) -> None:
    user = require_user(principal)
    with connect() as conn:
        _own(conn, user.id, project_id)
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# --------------------------------------------------------------------------- #
# Assistant chat
# --------------------------------------------------------------------------- #

def messages(principal: Principal, project_id: int) -> list[dict[str, Any]]:
    user = require_user(principal)
    with connect() as conn:
        _own(conn, user.id, project_id)
        return [dict(r) for r in conn.execute(
            "SELECT id, role, content, reply_to, created_at FROM project_messages WHERE project_id = ? ORDER BY id",
            (project_id,))]


def ask(principal: Principal, project_id: int, question: str) -> int:
    """Save a question. It is answered now if the report exists, or when it does."""
    user = require_user(principal)
    question = (question or "").strip()
    if not question:
        raise ValueError("Type a question first.")
    if len(question) > MAX_QUESTION:
        raise ValueError(f"Keep questions under {MAX_QUESTION} characters.")
    from .service import _check_rate  # shared per-account rate limits

    with connect(immediate=True) as conn:
        _own(conn, user.id, project_id)
        _check_rate(conn, "assistant", user.id)
        message_id = conn.execute(
            "INSERT INTO project_messages (project_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
            (project_id, question, now_iso())).lastrowid
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now_iso(), project_id))
    return int(message_id)


def pending_questions(project_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT q.id, q.content FROM project_messages q WHERE q.project_id = ? AND q.role = 'user' "
            "AND NOT EXISTS (SELECT 1 FROM project_messages a WHERE a.reply_to = q.id) ORDER BY q.id",
            (project_id,))]


def _history(project_id: int, before_id: int) -> list[dict[str, str]]:
    with connect() as conn:
        return [{"role": r["role"], "content": r["content"]} for r in conn.execute(
            "SELECT role, content FROM project_messages WHERE project_id = ? AND id < ? ORDER BY id",
            (project_id, before_id))]


def answer_pending(project_id: int, report: Report, *, provider: str, gateway_model: str = "") -> int:
    """Answer every unanswered question in order. Returns how many were answered.

    Used both by the background job (questions asked while the research ran) and by
    the page (questions asked afterwards). A failure is stored as the answer, so one
    bad call never leaves a question waiting forever.
    """
    from ..analysis import assistant

    answered = 0
    for question in pending_questions(project_id):
        try:
            reply = assistant.ask(report, question["content"], _history(project_id, question["id"]),
                                  provider=provider, gateway_model=gateway_model).text
        except Exception as exc:  # noqa: BLE001 - reported to the user in the chat
            log.warning("assistant failed on project %s: %s", project_id, exc)
            reply = f"Sorry - I couldn't answer that ({exc}). Ask again in a moment."
        with connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO project_messages (project_id, role, content, reply_to, created_at) "
                    "VALUES (?, 'assistant', ?, ?, ?)", (project_id, reply, question["id"], now_iso()))
            except Exception:  # noqa: BLE001 - answered concurrently by another worker
                continue
        answered += 1
    return answered
