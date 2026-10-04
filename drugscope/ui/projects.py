"""Research projects: the list, and each project's own page.

A project page is where a run happens: it shows the live progress while the research
runs in the background, then the report, with the research assistant alongside the
whole time. Each project has its own link (`?project=12`) that only its owner can
open.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import streamlit as st

from .. import jobs
from ..community import projects
from ..community.auth import PermissionDenied
from ..community.projects import ProjectNotFound
from ..config import RunSettings
from ..models import Report
from . import account
from . import components as ui
from . import nav

STATUS_BADGE = {"running": ("Running", "warning"), "done": ("Ready", "good"), "failed": ("Failed", "critical")}


# --------------------------------------------------------------------------- #
# Which project is open
# --------------------------------------------------------------------------- #

def active_project_id() -> int | None:
    value = st.query_params.get("project")
    if value and str(value).isdigit():
        st.session_state["project_id"] = int(value)
    return st.session_state.get("project_id")


def open_project(project_id: int, *, switch: bool = False) -> None:
    st.session_state["project_id"] = project_id
    if switch:
        st.switch_page(nav.PAGES["research"], query_params={"project": str(project_id)})
    st.query_params["project"] = str(project_id)


def close_project() -> None:
    st.session_state.pop("project_id", None)
    if "project" in st.query_params:
        del st.query_params["project"]


def _report(user, meta: dict[str, Any]) -> Report | None:
    """The project's report, parsed once per change rather than on every click."""
    if meta["status"] != "done":
        return None
    cache = st.session_state.setdefault("report_cache", {})
    key = (meta["id"], meta["updated_at"])
    if key not in cache:
        cache.clear()
        cache[key] = projects.load_report(user, meta["id"])
    return cache[key]


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #

def sidebar_recent() -> None:
    user = account.current_user()
    if user is None:
        return
    rows = projects.list_projects(user, limit=6)
    st.divider()
    st.markdown("**Projects**")
    current = st.session_state.get("project_id")
    for row in rows:
        badge = {"running": "⏳ ", "failed": "⚠ "}.get(row["status"], "")
        if st.button(badge + row["title"], key=f"side-proj-{row['id']}", width="stretch", help=row["title"],
                     type="secondary" if row["id"] == current else "tertiary"):
            open_project(row["id"])
            st.rerun()
    a, b = st.columns(2)
    if a.button("＋ New", key="side-new", width="stretch"):
        close_project()
        st.rerun()
    with b:
        nav.link("projects", "All")


# --------------------------------------------------------------------------- #
# Live progress
# --------------------------------------------------------------------------- #

@st.fragment(run_every=1.0)
def progress_panel(project_id: int) -> None:
    """Redrawn every second while the job runs; reloads the page when it ends."""
    job = jobs.active(project_id)
    if job is None or job.phase == "finished":
        st.rerun()  # whole page: show the report and the answers
        return
    events = list(job.events)
    latest = events[-1] if events else None
    pct = latest.pct if latest else 2
    if job.phase == "answering":
        st.progress(100, text="Report ready - answering your questions...")
        return
    st.progress(min(pct, 100), text=latest.label if latest else "Starting research...")

    log: list[str] = []
    for event in events:
        line = f"<b>{ui.esc(event.label)}</b>" + (
            f" <span style='color:var(--ds-ink-3)'>- {ui.esc(event.detail)}</span>" if event.detail else "")
        if not log or log[-1] != line:
            log.append(line)
    recent = log[-4:]
    lines = "".join(
        f"<div class='ds-runline{' is-current' if i == len(recent) - 1 else ''}'>"
        f"{'&rsaquo; ' if i == len(recent) - 1 else '&nbsp;&nbsp;'}{line}</div>"
        for i, line in enumerate(recent))
    elapsed = job.elapsed
    eta = ""
    if 38 <= pct < 100:
        eta = f" &middot; about {max(1, round(elapsed / pct * (100 - pct) / 60))} min left (rough estimate)"
    settings = job.settings
    with_sweep = settings.profile.web_search and settings.toggles.recent_news and settings.provider == "anthropic"
    stepper = ui.graph_stepper_markup(latest.stage if latest else "plan", with_sweep=with_sweep)
    st.markdown(
        f"<div class='ds-runlog'>{stepper}{lines}"
        f"<div class='ds-skel' style='width:{40 + (pct % 50)}%'></div>"
        f"<div style='margin-top:.5rem;font-size:.72rem;color:var(--ds-ink-3)'>"
        f"{elapsed:.0f}s elapsed &middot; {pct}%{eta} &middot; runs in the background - "
        f"you can ask questions or switch pages</div></div>",
        unsafe_allow_html=True,
    )

    found = next((e.payload for e in reversed(events)
                  if e.stage == "retrieve" and isinstance(e.payload, list) and e.payload), None)
    if found:
        with st.expander(f"Sources found ({len(found)}) - being read now", expanded=False):
            for record in [r for r in found if r.kind in ("paper", "trial")][:10]:
                st.markdown(
                    f"<div style='font-size:.8rem;margin-bottom:.25rem'><span class='ds-handle'>{ui.esc(record.sid)}</span> "
                    f"<a href='{ui.esc(record.url)}' target='_blank' rel='noopener'>{ui.esc(record.title[:140])}</a>"
                    f" <span style='color:var(--ds-ink-3)'>&middot; {ui.esc(record.source)}"
                    f"{' &middot; ' + str(record.year) if record.year else ''}</span></div>",
                    unsafe_allow_html=True)
    thinking = next((e.thinking for e in reversed(events) if e.thinking), "")
    if thinking:
        with st.expander("Model reasoning (live)", expanded=False):
            st.caption(thinking)


# --------------------------------------------------------------------------- #
# Assistant
# --------------------------------------------------------------------------- #

EXAMPLE_QUESTIONS = (
    "What is the strongest evidence here?",
    "Which trials are still recruiting?",
    "Summarise the safety concerns",
)


def assistant_panel(user, meta: dict[str, Any], report: Report | None, settings: RunSettings) -> None:
    pid = meta["id"]
    job = jobs.active(pid)
    ui.section("Research assistant", "ask anything about this research - answers come only from its report and sources")

    # Questions asked earlier and not answered yet: answer them now that the report exists.
    if report is not None and job is None and projects.pending_questions(pid):
        with st.spinner("The assistant is answering..."):
            projects.answer_pending(pid, report, provider=settings.provider, gateway_model=settings.gateway_model)

    history = projects.messages(user, pid)
    answered = {m["reply_to"] for m in history if m["role"] == "assistant"}
    records = {r.sid: r for r in report.records} if report else {}
    box = st.container(border=True, height=420 if history else "content")
    with box:
        if not history:
            with st.chat_message("assistant", avatar=":material/smart_toy:"):
                still = " It's still running - ask anyway, and I'll answer the moment it finishes." \
                    if meta["status"] == "running" else ""
                st.markdown(f"Hi! I've read this research and can answer questions about it.{still}")
        for message in history:
            if message["role"] == "user":
                with st.chat_message("user", avatar=":material/person:"):
                    st.markdown(ui.esc(message["content"]), unsafe_allow_html=True)
                if message["id"] not in answered:
                    with st.chat_message("assistant", avatar=":material/smart_toy:"):
                        if meta["status"] == "failed":
                            st.markdown("The research run failed, so there is no report to answer from. "
                                        "Run it again and ask once it's ready.")
                        else:
                            latest = job.latest if job else None
                            where = f" ({latest.pct}% - {latest.label})" if latest else ""
                            st.markdown(f"⏳ Waiting for the research to finish{ui.esc(where)}. "
                                        "I'll answer automatically.")
            else:
                with st.chat_message("assistant", avatar=":material/smart_toy:"):
                    st.markdown(ui.prose_markup(message["content"], records), unsafe_allow_html=True)

    if not history and meta["status"] != "failed":
        columns = st.columns(len(EXAMPLE_QUESTIONS))
        for column, example in zip(columns, EXAMPLE_QUESTIONS):
            if column.button(example, key=f"ask-{pid}-{example[:10]}", width="stretch"):
                _ask(user, pid, example)
    if meta["status"] != "failed":
        with st.container():
            question = st.chat_input("Ask about this research...", key=f"chat-{pid}", max_chars=projects.MAX_QUESTION)
        if question:
            _ask(user, pid, question)
    st.caption("The assistant answers only from this report and its sources, cites them, and does not give "
               "medical advice.")


def _ask(user, pid: int, question: str) -> None:
    try:
        projects.ask(user, pid, question)
    except Exception as exc:  # noqa: BLE001 - rate limit or validation, shown to the user
        st.error(str(exc))
        return
    st.rerun()


# --------------------------------------------------------------------------- #
# Project page
# --------------------------------------------------------------------------- #

def project_view(project_id: int, settings: RunSettings, *, render_report: Callable[[Report], None],
                 restart: Callable[[str, str], None]) -> None:
    user = account.current_user()
    if user is None:
        ui.masthead(meta=[])
        ui.note("Sign in to open your projects.", status="warning", label="Sign in required.")
        account.sign_in_panel("project")
        return
    try:
        meta = projects.get(user, project_id)
    except (ProjectNotFound, PermissionDenied):
        close_project()
        ui.masthead(meta=[])
        ui.note("That project does not exist or is not yours.", status="warning", label="Not found.")
        nav.link("projects", "Your projects", ":material/folder_open:")
        return

    report = _report(user, meta)
    summary = meta["summary"]
    ui.masthead(meta=[
        ("Sources", str(summary.get("sources", "-"))),
        ("Evidence quality", f"{summary['quality']}/100" if summary.get("quality") is not None else "-"),
        ("Runtime", f"{summary['elapsed']:.0f}s" if summary.get("elapsed") else "-"),
        ("Est. cost", f"${summary.get('cost', 0):.2f}"),
    ] if meta["status"] == "done" else [])

    label, tone = STATUS_BADGE[meta["status"]]
    st.markdown(
        f"<div class='ds-eyebrow'>Project #{meta['id']} · {ui.esc(meta['depth'])} research · "
        f"{ui.esc(meta['created_at'][:16].replace('T', ' '))} UTC</div>"
        f"<div class='ds-headline' style='font-size:1.7rem'>{ui.esc(meta['title'])}</div>"
        f"<div style='margin:-.2rem 0 1rem'>{ui.badge(label, status=tone)}</div>",
        unsafe_allow_html=True,
    )
    a, b, c, _ = st.columns([1, 1, 1, 3])
    if a.button("＋ New research", key="pv-new", width="stretch"):
        close_project()
        st.rerun()
    with b.popover("Rename", width="stretch"):
        title = st.text_input("Project name", value=meta["title"], key=f"pv-name-{project_id}")
        if st.button("Save", key=f"pv-save-{project_id}"):
            projects.rename(user, project_id, title)
            st.rerun()
    with c.popover("Delete", width="stretch"):
        st.caption("Deletes the report and its assistant conversation.")
        if st.button("Delete project", type="primary", key=f"pv-del-{project_id}"):
            projects.delete(user, project_id)
            close_project()
            st.rerun()

    job = jobs.active(project_id)
    if meta["status"] == "running":
        if job is None:
            ui.note("This run is no longer active. It may have been interrupted.", status="warning")
        else:
            progress_panel(project_id)
    elif meta["status"] == "failed":
        ui.note(meta["error"] or "The run failed.", status="critical", label="Run failed.")
        if st.button("Run this research again", type="primary", key=f"pv-retry-{project_id}"):
            restart(meta["query"], meta["depth"])

    assistant_panel(user, meta, report, settings)

    if report is not None:
        st.divider()
        render_report(report)


# --------------------------------------------------------------------------- #
# Projects page
# --------------------------------------------------------------------------- #

def projects_page() -> None:
    account.sidebar_status()
    ui.masthead(meta=[])
    st.markdown(
        "<div class='ds-eyebrow'>Projects</div><div class='ds-headline'>Your research projects</div>"
        "<div class='ds-subhead'>Every research run is saved here with its report and its assistant "
        "conversation. Only you can see them.</div>",
        unsafe_allow_html=True,
    )
    user = account.require_sign_in("Projects are saved to your account.")
    if user is None:
        return
    left, right = st.columns([3, 1])
    search = left.text_input("Search projects", key="pj-search", placeholder="Search by title",
                             label_visibility="collapsed")
    if right.button("＋ New research", type="primary", width="stretch", key="pj-new"):
        close_project()
        st.switch_page(nav.PAGES["research"])

    rows = projects.list_projects(user, search=search)
    if not rows:
        st.caption("No projects yet - run your first research from the Research page." if not search
                   else "No projects match that search.")
        return
    for row in rows:
        label, tone = STATUS_BADGE[row["status"]]
        summary = row["summary"]
        facts = [row["created_at"][:10], row["depth"]]
        if summary.get("sources"):
            facts.append(f"{summary['sources']} sources")
        if summary.get("quality") is not None:
            facts.append(f"evidence quality {summary['quality']}/100")
        facts.append(f"{row['questions']} question(s) asked")
        with st.container(border=True):
            info, actions = st.columns([5, 1])
            with info:
                st.markdown(
                    f"<div style='display:flex;gap:.6rem;align-items:center;flex-wrap:wrap'>"
                    f"<b style='color:var(--ds-ink);font-size:.98rem'>{ui.esc(row['title'])}</b>"
                    f"{ui.badge(label, status=tone)}</div>"
                    f"<div style='font-size:.75rem;color:var(--ds-ink-3);margin:.2rem 0'>{ui.esc(' · '.join(facts))}</div>"
                    + (f"<div style='font-size:.84rem;color:var(--ds-ink-2)'>{ui.esc(summary['verdict'])}</div>"
                       if summary.get("verdict") else "")
                    + (f"<div style='font-size:.8rem;color:var(--ds-critical)'>{ui.esc(row['error'][:200])}</div>"
                       if row["status"] == "failed" else ""),
                    unsafe_allow_html=True,
                )
            with actions:
                if st.button("Open", key=f"pj-open-{row['id']}", type="primary", width="stretch"):
                    open_project(row["id"], switch=True)
                with st.popover("Delete", width="stretch"):
                    if st.button("Delete project", key=f"pj-del-{row['id']}", type="primary"):
                        projects.delete(user, row["id"])
                        if st.session_state.get("project_id") == row["id"]:
                            close_project()
                        st.rerun()


def wait_for(project_id: int, timeout: float = 60.0) -> None:
    """Test helper: block until a project's background job is done."""
    jobs.wait(project_id, timeout)
    deadline = time.monotonic() + timeout
    while jobs.active(project_id) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
