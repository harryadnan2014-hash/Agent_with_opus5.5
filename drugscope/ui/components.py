"""Presentational components.

Small, stateless render functions. Two conventions worth noting:

* **A KPI card is a stat tile, not a chart.** A single headline number gets typographic
  hierarchy - label, value, context line, and optionally a meter when the number is a
  bounded score. It never becomes a gauge or a donut.
* **Text wears ink colours, never a series colour.** A status colour appears as a dot,
  a rule or a meter fill beside the text, always paired with a word, so meaning never
  rests on hue alone.
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable

import streamlit as st

from ..models import (
    DESIGN_LABELS,
    EvidenceConflict,
    KeyFinding,
    ResearchGap,
    SafetySignal,
    SourceRecord,
    TimelineEvent,
)
from . import theme


def _esc(value: Any) -> str:
    # `$` is escaped too: Streamlit's markdown pass would otherwise read a pair of
    # dollar amounts as a LaTeX formula.
    return html.escape(str(value), quote=True).replace("$", "&#36;")


# Public alias for page code that builds its own small HTML snippets.
esc = _esc


def _compact(markup: str) -> str:
    """Flatten generated markup to a single line before handing it to markdown.

    Streamlit renders `unsafe_allow_html` through a markdown parser, and markdown
    treats an indented line as a code block and a blank line as the end of an HTML
    block. Readable, indented f-string templates therefore render as visible source
    unless the indentation and newlines are stripped first. HTML does not care.
    """
    joined = " ".join(line.strip() for line in markup.splitlines() if line.strip())
    # Joining with a space, not "", keeps word boundaries intact where prose wraps
    # across source lines - otherwise "trials and" + "regulatory records" collapses
    # into "andregulatory". The cosmetic space that leaves between adjacent tags is
    # then removed.
    return re.sub(r">\s+<", "><", joined)


def _write(markup: str) -> None:
    st.markdown(_compact(markup), unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Chrome
# --------------------------------------------------------------------------- #

def _flask(size: int) -> str:
    """The brand mark, shared with the sidebar so the logo is one shape."""
    from .home import icon
    return icon("flask", size=size, stroke=1.9)


def masthead(*, meta: list[tuple[str, str]] | None = None) -> None:
    from ..config import APP_NAME, APP_TAGLINE

    blocks = "".join(
        f"<div><span>{_esc(label)}</span><b>{_esc(value)}</b></div>"
        for label, value in (meta or [])
    )
    _write(
        f"""
        <div class="ds-masthead">
          <div>
            <div class="ds-wordmark">
              <span class="ds-logo" style="width:30px;height:30px">{_flask(17)}</span>
              {_esc(APP_NAME)}
            </div>
            <div class="ds-tagline">{_esc(APP_TAGLINE)}</div>
          </div>
          <div class="ds-masthead-meta">{blocks}</div>
        </div>
        """
    )


def section(title: str, note: str = "") -> None:
    _write(
        f"<div class='ds-section'><h3>{_esc(title)}</h3>"
        f"<span>{_esc(note)}</span></div>"
    )


def badge(label: str, *, status: str = "", slot: int | None = None, dot: bool = True) -> str:
    """Returns markup rather than rendering, so badges can be composed inline."""
    t = theme.tokens()
    if status:
        color = theme.status_color(status)
    elif slot is not None:
        color = t["series"][slot % len(t["series"])]
    else:
        color = t["text_muted"]

    marker = f"<span class='ds-badge-dot' style='background:{color}'></span>" if dot else ""
    return (
        f"<span class='ds-badge' style='--ds-badge-ink:{color};"
        f"--ds-badge-line:{color}44;--ds-badge-bg:{color}14'>"
        f"{marker}{_esc(label)}</span>"
    )


def note(text: str, *, status: str = "warning", label: str = "") -> None:
    color = theme.status_color(status)
    prefix = f"<b>{_esc(label)}</b> " if label else ""
    _write(f"<div class='ds-note' style='--ds-note-color:{color}'>{prefix}{_esc(text)}</div>")


# --------------------------------------------------------------------------- #
# KPI cards
# --------------------------------------------------------------------------- #

def kpi(
    label: str,
    value: Any,
    *,
    unit: str = "",
    sub: str = "",
    status: str = "",
    slot: int | None = 0,
    meter: float | None = None,
    help_text: str = "",
    delay: int = 0,
) -> None:
    """One stat tile.

    `meter` is a 0-1 fill, used only where the value is a bounded score - an index
    out of 100, a share of a total. A count with no natural ceiling gets no meter,
    because a bar that can never fill is noise.
    """
    t = theme.tokens()
    if status:
        accent = theme.status_color(status)
    elif slot is not None:
        accent = t["series"][slot % len(t["series"])]
    else:
        accent = t["border_strong"]

    unit_markup = f"<span class='ds-unit'>{_esc(unit)}</span>" if unit else ""
    meter_markup = ""
    if meter is not None:
        width = max(0.0, min(1.0, meter)) * 100
        meter_markup = (
            f"<div class='ds-meter'><span style='width:{width:.1f}%;"
            f"background:{accent}'></span></div>"
        )
    hint = (
        f"<span style='color:{t['text_muted']};cursor:help' title='{_esc(help_text)}'>&#9432;</span>"
        if help_text else ""
    )
    sub_markup = f"<div class='ds-kpi-sub'>{_esc(sub)}</div>" if sub else ""

    _write(
        f"""
        <div class="ds-kpi" style="--ds-kpi-accent:{accent};animation-delay:{delay * 60}ms">
          <div class="ds-kpi-label">{_esc(label)}{hint}</div>
          <div class="ds-kpi-value">{_esc(value)}{unit_markup}</div>
          {meter_markup}
          {sub_markup}
        </div>
        """
    )


def kpi_row(cards: list[dict[str, Any]], *, per_row: int = 4) -> None:
    """Filters and KPI rows sit above the charts, in one row, per the layout rule."""
    for start in range(0, len(cards), per_row):
        chunk = cards[start : start + per_row]
        columns = st.columns(len(chunk), gap="small")
        for index, (column, card) in enumerate(zip(columns, chunk)):
            with column:
                kpi(**card, delay=index)


# --------------------------------------------------------------------------- #
# Report blocks
# --------------------------------------------------------------------------- #

def verdict(text: str, *, confidence: str, rationale: str) -> None:
    status = theme.CONFIDENCE_STATUS.get(confidence, "warning")
    _write(
        f"""
        <div class="ds-verdict">
          <div class="ds-verdict-label">Bottom line</div>
          <div class="ds-verdict-text">{_esc(text)}</div>
          <div style="margin-top:.75rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
            {badge(f"{confidence.title()} confidence", status=status)}
            <span style="font-size:.79rem;color:var(--ds-ink-2)">{_esc(rationale)}</span>
          </div>
        </div>
        """
    )


def _chip(sid: str, record: SourceRecord) -> str:
    title = f"{record.citation} - {record.title}"
    if not record.url:
        # Uploaded documents have no address; a link to "" would reload the app.
        return f"<span class='ds-handle' title='{_esc(title)}'>{_esc(sid)}</span>"
    return (
        f"<a href='{_esc(record.url)}' target='_blank' rel='noopener' "
        f"class='ds-handle' title='{_esc(title)}' "
        f"style='text-decoration:none'>{_esc(sid)}</a>"
    )


def citations_markup(sids: Iterable[str], records: dict[str, SourceRecord]) -> str:
    return " ".join(_chip(sid, records[sid]) for sid in sids if sid in records)


_INLINE_HANDLE = re.compile(r"\[([A-Z]{1,2}\d{1,4}(?:\s*[,;]\s*[A-Z]{1,2}\d{1,4})*)\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _linkify(escaped: str, records: dict[str, SourceRecord]) -> str:
    """Swap `[S4]` / `[S4, T2]` in already-escaped text for source chips."""
    def replace(match: re.Match[str]) -> str:
        handles = re.split(r"\s*[,;]\s*", match.group(1))
        if not all(h in records for h in handles):
            return match.group(0)
        return "".join(_chip(h, records[h]) for h in handles)

    return _INLINE_HANDLE.sub(replace, escaped)


def prose_markup(text: str, records: dict[str, SourceRecord]) -> str:
    """Model prose as HTML paragraphs, with citation handles made clickable.

    Rendered as HTML rather than through `st.markdown` on purpose: model text
    routinely contains dollar amounts, and markdown reads `$1.2bn ... $3` as a
    LaTeX formula and typesets the sentence between them as maths.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    rendered = []
    for paragraph in paragraphs:
        body = _esc(paragraph).replace("\n", "<br>")
        body = _BOLD.sub(r"<b>\1</b>", body)
        rendered.append(f"<p>{_linkify(body, records)}</p>")
    return f"<div class='ds-prose'>{''.join(rendered)}</div>"


def prose(text: str, records: dict[str, SourceRecord]) -> None:
    if not (text or "").strip():
        note("The model returned nothing for this section.", status="warning")
        return
    _write(prose_markup(text, records))


def setup_card(*, provider_label: str, env_var: str, hints: list[str]) -> None:
    """Shown on the home screen until a model provider has a key."""
    hint_markup = "".join(f"<li><b>Found a likely typo:</b> {_esc(h).replace('`', '')}</li>" for h in hints)
    _write(
        f"""
        <div class="ds-setup">
          <b>Connect a model to start researching.</b> No key was found for
          {_esc(provider_label)}. The research databases need no key; the analysis does.
          <ol>
            {hint_markup}
            <li>Open the <code>.env</code> file in the project folder (copy
                <code>.env.example</code> if it does not exist).</li>
            <li>Add one line, e.g. <code>{_esc(env_var)}=your-key</code> - OpenRouter
                (<code>OPENROUTER_API_KEY</code>) has free models.</li>
            <li>Restart <code>streamlit run app.py</code> so the key is picked up.</li>
          </ol>
        </div>
        """
    )


def finding_card(
    finding: KeyFinding,
    records: dict[str, SourceRecord],
    *,
    index: int,
    numbers: dict[str, Any] | None = None,
) -> None:
    status = theme.STRENGTH_STATUS.get(finding.strength, "warning")
    accent = theme.status_color(status)

    if finding.citations:
        support = (
            f"<span style='color:var(--ds-ink-3)'>Evidence</span> "
            f"{citations_markup(finding.citations, records)}"
        )
    else:
        support = badge("Unsupported - no resolvable citation", status="critical")

    # The number check: did every figure in this finding come from what it cites?
    if numbers and numbers.get("checked"):
        unverified = numbers.get("unverified") or []
        if unverified:
            support += " " + badge(
                f"Not found in cited sources: {', '.join(unverified[:4])} - may be derived, check before quoting",
                status="warning")
        else:
            support += " " + badge(f"All {numbers['checked']} figures match cited sources", status="good")

    _write(
        f"""
        <div class="ds-card" style="--ds-card-accent:{accent};animation-delay:{min(index, 8) * 55}ms">
          <div class="ds-card-head">
            <div class="ds-card-title">{index}. {_esc(finding.headline)}</div>
            {badge(finding.strength.title(), status=status)}
          </div>
          <div class="ds-card-body">{_linkify(_esc(finding.detail), records)}</div>
          <div class="ds-sowhat">{_esc(finding.so_what)}</div>
          <div class="ds-card-foot">{support}</div>
        </div>
        """
    )


def conflict_card(conflict: EvidenceConflict, records: dict[str, SourceRecord]) -> None:
    t = theme.tokens()
    status = theme.SEVERITY_STATUS.get(conflict.severity, "warning")
    severity_label = conflict.severity.replace("_", " ").title()

    _write(
        f"""
        <div class="ds-conflict">
          <div class="ds-conflict-head">
            <span>{_esc(conflict.topic.title())}</span>
            {badge(severity_label, status=status)}
          </div>
          <div class="ds-sides">
            <div class="ds-side">
              <div class="ds-side-label" style="color:{t['diverging_low']}">Position A</div>
              {_esc(conflict.position_a)}
              <div style="margin-top:.5rem">{citations_markup(conflict.citations_a, records)}</div>
            </div>
            <div class="ds-side">
              <div class="ds-side-label" style="color:{t['diverging_high']}">Position B</div>
              {_esc(conflict.position_b)}
              <div style="margin-top:.5rem">{citations_markup(conflict.citations_b, records)}</div>
            </div>
          </div>
          <div class="ds-adjudication">
            <b>Assessment.</b> {_esc(conflict.assessment)}<br>
            <span style="color:var(--ds-ink-2)"><b>Likely cause of divergence.</b>
            {_esc(conflict.likely_explanation)}</span>
          </div>
        </div>
        """
    )


def gap_card(gap: ResearchGap, *, index: int) -> None:
    status = {"critical": "critical", "high": "serious", "moderate": "warning"}.get(
        gap.priority, "warning"
    )
    accent = theme.status_color(status)
    _write(
        f"""
        <div class="ds-card" style="--ds-card-accent:{accent};animation-delay:{min(index, 8) * 55}ms">
          <div class="ds-card-head">
            <div class="ds-card-title">{index}. {_esc(gap.question)}</div>
            {badge(f"{gap.priority.title()} priority", status=status)}
          </div>
          <div class="ds-card-body">
            <b style="color:var(--ds-ink)">Why it matters.</b> {_esc(gap.why_it_matters)}<br>
            <b style="color:var(--ds-ink)">Missing from the corpus.</b> {_esc(gap.evidence_absent)}
          </div>
          <div class="ds-sowhat">
            <b>What would answer it.</b> {_esc(gap.what_would_answer_it)}
          </div>
        </div>
        """
    )


def safety_card(signal: SafetySignal, records: dict[str, SourceRecord]) -> None:
    status = theme.SEVERITY_STATUS.get(signal.seriousness, "warning")
    accent = theme.status_color(status)
    frequency = f" &middot; {_esc(signal.frequency)}" if signal.frequency.strip() else ""
    _write(
        f"""
        <div class="ds-card" style="--ds-card-accent:{accent}">
          <div class="ds-card-head">
            <div class="ds-card-title">{_esc(signal.event)}</div>
            {badge(signal.seriousness.title(), status=status)}
          </div>
          <div class="ds-card-body">{_esc(signal.context)}{frequency}</div>
          <div class="ds-card-foot">{citations_markup(signal.citations, records)}</div>
        </div>
        """
    )


_EVENT_SLOT = {
    "regulatory": 0,
    "trial": 2,
    "publication": 6,
    "safety": 7,
    "commercial": 1,
    "discovery": 4,
    "preclinical": 3,
    "other": 5,
}


def timeline(events: list[TimelineEvent], records: dict[str, SourceRecord]) -> None:
    if not events:
        note("No dated events could be assembled from the retrieved records.", status="warning")
        return

    t = theme.tokens()
    ordered = sorted(events, key=lambda e: e.date or "9999")
    rows: list[str] = []

    for position, event in enumerate(ordered):
        color = t["series"][_EVENT_SLOT.get(event.category, 5) % len(t["series"])]
        rows.append(
            f"""
            <div class="ds-event" style="--ds-event-color:{color};
                 animation-delay:{min(position, 12) * 45}ms">
              <div class="ds-event-date">{_esc(event.date)}
                &nbsp;&middot;&nbsp;{_esc(event.category.title())}</div>
              <div class="ds-event-label">{_esc(event.label)}</div>
              <div class="ds-event-detail">{_esc(event.detail)}</div>
              <div style="margin-top:.35rem">{citations_markup(event.citations, records)}</div>
            </div>
            """
        )

    _write(f"<div class='ds-timeline'>{''.join(rows)}</div>")


def source_row(record: SourceRecord) -> None:
    tier_slot = theme.TIER_COLOR_SLOT.get(record.tier, 4)
    chips = [badge(record.tier, slot=tier_slot), badge(record.source, dot=False)]

    if record.kind == "trial":
        chips.append(badge(record.meta.get("phase", "-"), dot=False))
        chips.append(badge(record.meta.get("status", "-"), dot=False))
    else:
        chips.append(badge(DESIGN_LABELS.get(record.design, record.design), dot=False))
    if record.sample_size:
        chips.append(badge(f"n={record.sample_size:,}", dot=False))
    if record.meta.get("citations"):
        chips.append(badge(f"{record.meta['citations']:,} citations", dot=False))

    identifiers = " &middot; ".join(
        f"{k.upper()} {_esc(v)}" for k, v in list(record.identifiers.items())[:3]
    )
    title = (
        f'<a href="{_esc(record.url)}" target="_blank" rel="noopener">{_esc(record.title)}</a>'
        if record.url else f'<b style="color:var(--ds-ink)">{_esc(record.title)}</b>'
    )

    _write(
        f"""
        <div class="ds-source">
          <span class="ds-handle">{_esc(record.sid)}</span>
          {title}
          <div class="ds-source-meta">
            <span>{_esc(record.citation)}</span>{' &middot; ' + identifiers if identifiers else ''}
          </div>
          <div class="ds-source-meta">{''.join(chips)}</div>
        </div>
        """
    )


# Graph nodes by step. `appraise` and `sweep` are parallel branches of one step.
GRAPH_STEPS: list[tuple[str, ...]] = [
    ("plan",), ("retrieve",), ("appraise", "sweep"), ("measure",), ("synthesise",), ("audit",),
]
_STAGE_STEP = {"plan": 0, "retrieve": 1, "appraise": 2, "sweep": 2, "measure": 3,
               "synthesise": 4, "audit": 5, "done": 6}


def graph_stepper_markup(stage: str, *, with_sweep: bool) -> str:
    """The LangGraph nodes of a run, with the step executing now highlighted."""
    current = _STAGE_STEP.get(stage, 0)

    def pill(name: str, step: int) -> str:
        state = "is-done" if step < current else "is-live" if step == current else "is-todo"
        return f"<span class='ds-node {state}'>{_esc(name)}</span>"

    columns: list[str] = []
    for step, names in enumerate(GRAPH_STEPS):
        names = tuple(n for n in names if with_sweep or n != "sweep")
        pills = "".join(pill(n, step) for n in names)
        columns.append(f"<span class='ds-node-par'>{pills}</span>" if len(names) > 1 else pills)
    arrow = "<span class='ds-node-arrow'>&rarr;</span>"
    return (
        "<div class='ds-graph'><span class='ds-graph-label'>LangGraph</span>"
        + arrow.join(columns)
        + "</div>"
    )


def spend_line(usage: Any, elapsed: float, *, depth: str) -> None:
    _write(
        f"""
        <div style="display:flex;gap:1.6rem;flex-wrap:wrap;font-size:.75rem;
                    color:var(--ds-ink-3);padding:.6rem 0;
                    border-top:1px solid var(--ds-border);margin-top:1.4rem">
          <span>Depth <b style="color:var(--ds-ink-2)">{_esc(depth)}</b></span>
          <span>Runtime <b style="color:var(--ds-ink-2)">{elapsed:.0f}s</b></span>
          <span>Model calls <b style="color:var(--ds-ink-2)">{usage.calls}</b></span>
          <span>Input <b style="color:var(--ds-ink-2)">{usage.input_tokens:,}</b> tok</span>
          <span>Output <b style="color:var(--ds-ink-2)">{usage.output_tokens:,}</b> tok</span>
          <span>Cache read <b style="color:var(--ds-ink-2)">{usage.cache_read_tokens:,}</b> tok</span>
          <span>Est. cost <b style="color:var(--ds-ink-2)">${usage.cost_usd:.2f}</b></span>
        </div>
        """
    )
