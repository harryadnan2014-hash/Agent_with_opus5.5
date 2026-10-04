"""The home screen.

Everything a visitor sees before they have run anything: the brand, the search
hero, what the product does, and the shape of the report they will get back.

It is built as self-contained HTML blocks rather than Streamlit widgets so the
layout is exact - a card grid with coloured icon tiles is not something the widget
set produces. The only live widget on this screen is the search form itself, which
`app.py` owns.

The icon set is inline SVG on purpose. Streamlit's own Material icon font fails to
load offline and prints its ligature name ("_arrow_right") as visible text, which
is exactly the kind of detail that makes a product look unfinished.
"""

from __future__ import annotations

import html
import re
from typing import Any

import streamlit as st

from . import theme

# --------------------------------------------------------------------------- #
# Icons - 24x24 stroke paths, inherit currentColor
# --------------------------------------------------------------------------- #

ICONS: dict[str, str] = {
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "paper": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
             '<path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/>',
    "flask": '<path d="M10 3h4M10 3v6L5 18a2 2 0 0 0 1.7 3h10.6A2 2 0 0 0 19 18l-5-9V3"/>'
             '<path d="M7.5 14h9"/>',
    "bank": '<path d="M3 10h18L12 4 3 10z"/><path d="M5 10v8M10 10v8M14 10v8M19 10v8"/>'
            '<path d="M3 20h18"/>',
    "compare": '<path d="M4 6h6M4 18h6M14 6h6M14 18h6"/><path d="M7 6v12M17 6v12"/>'
               '<path d="m10 12 4 0"/>',
    "trend": '<path d="m3 17 6-6 4 4 8-8"/><path d="M15 7h6v6"/>',
    "question": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.7"/>'
                '<path d="M12 17h.01"/>',
    "report": '<path d="M5 3h9l5 5v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/>'
              '<path d="M14 3v5h5"/><path d="M8 13h5M8 17h8"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "arrow": '<path d="M5 12h13"/><path d="m13 6 6 6-6 6"/>',
    "pill": '<rect x="3" y="8" width="18" height="8" rx="4" transform="rotate(-45 12 12)"/>'
            '<path d="M8.5 8.5 15.5 15.5"/>',
    "shield": '<path d="M12 3 5 6v6c0 4.4 3 8.2 7 9 4-0.8 7-4.6 7-9V6l-7-3z"/>',
}


def icon(name: str, *, size: int = 20, stroke: float = 1.8) -> str:
    body = ICONS.get(name, ICONS["search"])
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="currentColor" stroke-width="{stroke}" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


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
# Brand
# --------------------------------------------------------------------------- #

def logo_markup(size: int = 34) -> str:
    """Gradient flask mark plus the two-tone wordmark."""
    t = theme.tokens()
    return (
        f'<span class="ds-logo" style="width:{size}px;height:{size}px">'
        f'{icon("flask", size=int(size * 0.55), stroke=1.9)}</span>'
        f'<span class="ds-wordtext">Drug<span>Scope</span></span>'
    )


def sidebar_brand() -> None:
    _write(
        f'<div class="ds-brand">{logo_markup(34)}</div>'
        f'<div class="ds-brand-sub">AI-powered pharmaceutical<br>research agent</div>'
    )


def sidebar_footer() -> None:
    _write(
        '<div class="ds-brand-foot">'
        f'<span>{icon("shield", size=16)}</span>'
        '<div>Smarter research.<br>Better treatments.</div>'
        '</div>'
    )


# --------------------------------------------------------------------------- #
# Hero
# --------------------------------------------------------------------------- #

def hero() -> None:
    t = theme.tokens()
    _write(
        f"""
        <div class="ds-welcome">
          <div class="ds-welcome-art"></div>
          <div class="ds-welcome-body">
            <div class="ds-welcome-eyebrow">Welcome to</div>
            <div class="ds-welcome-mark">Drug<span>Scope</span></div>
            <div class="ds-welcome-lead">
              Your AI research agent for drugs, diseases and treatments.
            </div>
            <div class="ds-welcome-sub">
              Search, analyse and compare scientific papers, clinical trials and
              regulatory records &mdash; then get one structured report where every
              claim links back to a real source.
            </div>
          </div>
        </div>
        """
    )


# --------------------------------------------------------------------------- #
# Capability grid
# --------------------------------------------------------------------------- #

CAPABILITIES: list[dict[str, str]] = [
    {"icon": "search", "slot": "0", "title": "Research any drug,<br>disease or treatment",
     "body": "Ask in plain language. The question becomes real database queries."},
    {"icon": "paper", "slot": "1", "title": "Search &amp; analyse<br>scientific papers",
     "body": "Europe PMC and PubMed, appraised record by record."},
    {"icon": "flask", "slot": "2", "title": "Find &amp; compare<br>clinical trials",
     "body": "Phases, status, enrolment and posted results."},
    {"icon": "bank", "slot": "3", "title": "Check regulatory<br>approvals &amp; updates",
     "body": "Approval history, labels, boxed warnings and recalls."},
    {"icon": "compare", "slot": "4", "title": "Compare competing<br>drugs &amp; treatments",
     "body": "A head-to-head matrix across the criteria that decide."},
    {"icon": "trend", "slot": "5", "title": "Track recent<br>developments",
     "body": "What changed lately, and whether it is peer reviewed."},
    {"icon": "question", "slot": "6", "title": "Identify gaps &amp;<br>unanswered questions",
     "body": "What the evidence conspicuously does not cover."},
    {"icon": "report", "slot": "7", "title": "Generate an evidence<br>backed report",
     "body": "Structured, cited and exportable in one pass."},
]


def capability_grid(items: list[dict[str, str]] | None = None) -> None:
    t = theme.tokens()
    cards = items if items is not None else CAPABILITIES
    blocks: list[str] = []

    for position, item in enumerate(cards):
        color = t["series"][int(item["slot"]) % len(t["series"])]
        blocks.append(
            f"""
            <div class="ds-cap" style="--cap:{color};animation-delay:{position * 45}ms">
              <div class="ds-cap-icon">{icon(item['icon'], size=21)}</div>
              <div class="ds-cap-title">{item['title']}</div>
              <div class="ds-cap-body">{item['body']}</div>
              <div class="ds-cap-arrow">{icon('arrow', size=16)}</div>
            </div>
            """
        )

    _write(f'<div class="ds-capgrid">{"".join(blocks)}</div>')


# --------------------------------------------------------------------------- #
# Side panels
# --------------------------------------------------------------------------- #

def panel_open(title: str, icon_name: str, *, meta: str = "") -> str:
    right = f'<span class="ds-panel-meta">{_esc(meta)}</span>' if meta else ""
    return (
        f'<div class="ds-panel"><div class="ds-panel-head">'
        f'<span class="ds-panel-icon">{icon(icon_name, size=17)}</span>'
        f'<b>{_esc(title)}</b>{right}</div><div class="ds-panel-body">'
    )


PANEL_CLOSE = "</div></div>"


def what_you_get() -> None:
    """The shape of a finished report, as a preview of the deliverable."""
    t = theme.tokens()
    rows = [
        ("paper", 0, "Scientific papers", "appraised individually"),
        ("flask", 2, "Clinical trials", "phase, status, enrolment"),
        ("bank", 3, "Regulatory records", "approvals and labels"),
        ("compare", 4, "Comparative analysis", "head-to-head matrix"),
        ("trend", 1, "Key findings", "graded by evidence strength"),
        ("question", 6, "Open questions", "gaps worth closing"),
    ]
    body = "".join(
        f'<div class="ds-prow" style="--cap:{t["series"][slot % len(t["series"])]}">'
        f'<span class="ds-prow-icon">{icon(name, size=16)}</span>'
        f'<span class="ds-prow-label">{_esc(label)}</span>'
        f'<span class="ds-prow-meta">{_esc(meta)}</span></div>'
        for name, slot, label, meta in rows
    )
    _write(panel_open("What you get back", "report") + body + PANEL_CLOSE)


def how_it_works() -> None:
    steps = [
        ("Plan", "Your question becomes boolean database queries"),
        ("Retrieve", "Nine open research databases, queried in parallel"),
        ("Appraise ‖ Sweep", "Every record read for design, size and claims - "
                                  "with the web sweep as a parallel branch"),
        ("Measure", "Indices computed in code, not guessed by a model"),
        ("Synthesise", "One pass over the structured evidence"),
        ("Audit", "Every citation checked against what was retrieved"),
    ]
    body = "".join(
        f'<div class="ds-step-row"><b>{i}</b>'
        f'<div><span>{_esc(name)}</span><em>{_esc(detail)}</em></div></div>'
        for i, (name, detail) in enumerate(steps, 1)
    )
    _write(panel_open("How it works", "clock", meta="LangGraph pipeline") + body + PANEL_CLOSE)


def trust_note() -> None:
    _write(
        '<div class="ds-trust">'
        '<b>Citations are retrieved, not generated.</b> Every source is fetched from a '
        'public research database before analysis begins, and any reference the model '
        'invents is stripped before you see the report.'
        '</div>'
    )
