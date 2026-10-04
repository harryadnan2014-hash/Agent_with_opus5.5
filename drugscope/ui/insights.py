"""Insights - exploratory analysis of the community research data.

The public page reads approved reports only; the administrator's version (in the
admin workspace) reads everything and adds moderation and engagement detail. Both
render through `render`, so the two can never drift apart in how they present a
number. Every chart is a count of reports or votes from self-selected people, and the
page says so: none of these are rates, and none are risk.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go
import streamlit as st

from ..community import service
from . import account, theme
from . import components as ui
from . import nav
from .charts import PLOTLY_CONFIG

TYPE_LABELS = {"side_effect": "Side effect", "interaction": "Food interaction",
               "drug_interaction": "Drug interaction"}


def _bar(rows: list[dict[str, Any]], label: str, value: str, *, key: str, order: list[str] | None = None,
         horizontal: bool = True, slot: int = 0, height: int | None = None) -> None:
    if not rows:
        st.caption("No data yet.")
        return
    if order:
        rank = {name: i for i, name in enumerate(order)}
        rows = sorted(rows, key=lambda r: rank.get(r[label], 99))
    t = theme.tokens()
    names = [str(r[label]) for r in rows]
    values = [r[value] for r in rows]
    bar = go.Bar(
        x=values if horizontal else names, y=names if horizontal else values,
        orientation="h" if horizontal else "v",
        marker={"color": t["series"][slot % len(t["series"])], "cornerradius": 4},
        text=values, textposition="outside", cliponaxis=False,
        textfont={"size": 11, "color": t["text_muted"]},
        hovertemplate="<b>%{" + ("y" if horizontal else "x") + "}</b><br>%{" + ("x" if horizontal else "y")
                      + "}<extra></extra>",
    )
    layout = theme.plotly_layout(height or (max(170, 30 * len(rows)) if horizontal else 230))
    if horizontal:
        layout["yaxis"]["autorange"] = "reversed"
    st.plotly_chart(go.Figure(bar, layout=layout), width="stretch", config=PLOTLY_CONFIG, key=key)


def _weekly(data: dict[str, Any], key: str) -> None:
    reports = {r["week"]: r["reports"] for r in data["reports_by_week"]}
    votes = {r["week"]: r["votes"] for r in data["votes_by_week"]}
    weeks = sorted(set(reports) | set(votes))
    if not weeks:
        st.caption("No activity yet.")
        return
    t = theme.tokens()
    figure = go.Figure()
    figure.add_bar(x=weeks, y=[reports.get(w, 0) for w in weeks], name="Reports",
                   marker={"color": t["series"][0], "cornerradius": 4})
    figure.add_bar(x=weeks, y=[votes.get(w, 0) for w in weeks], name="Votes",
                   marker={"color": t["series"][1], "cornerradius": 4})
    layout = theme.plotly_layout(240, showlegend=True)
    layout["barmode"] = "group"
    layout["xaxis"]["title"] = {"text": "Week starting", "font": layout["xaxis"]["title"]["font"]}
    figure.update_layout(**layout)
    st.plotly_chart(figure, width="stretch", config=PLOTLY_CONFIG, key=key)


def _confirmed(rows: list[dict[str, Any]], key: str) -> None:
    rows = [r for r in rows if r["yes_votes"] + r["no_votes"]]
    if not rows:
        st.caption("No votes yet.")
        return
    t = theme.tokens()

    def title(r: dict[str, Any]) -> str:
        if r["kind"] == "side_effect":
            return f"{r['medication']} → {r['symptom']}"
        return f"{r['medication']} + {r['food'] or r['other_medication']}"

    names = [title(r) for r in rows]
    figure = go.Figure()
    figure.add_bar(y=names, x=[r["yes_votes"] for r in rows], orientation="h", name="Experienced it too",
                   marker={"color": t["series"][2], "cornerradius": 4})
    figure.add_bar(y=names, x=[r["no_votes"] for r in rows], orientation="h", name="Haven't experienced it",
                   marker={"color": t["surface_inset"], "cornerradius": 4})
    layout = theme.plotly_layout(max(200, 34 * len(rows)), showlegend=True)
    layout["barmode"] = "stack"
    layout["yaxis"]["autorange"] = "reversed"
    figure.update_layout(**layout)
    st.plotly_chart(figure, width="stretch", config=PLOTLY_CONFIG, key=key)
    st.dataframe([{
        "Entry": title(r), "Approved reports": r["reports"], "Experienced too": r["yes_votes"],
        "Haven't": r["no_votes"], "Respondents": r["yes_votes"] + r["no_votes"],
        "Said 'experienced too'": f"{100 * r['yes_votes'] / (r['yes_votes'] + r['no_votes']):.0f}%",
    } for r in rows], hide_index=True, width="stretch")


def render(data: dict[str, Any], *, key: str, admin: bool = False) -> None:
    votes = data["votes"]
    share = f"{100 * votes['yes'] / votes['total']:.0f}% said 'experienced too'" if votes["total"] else "no votes yet"
    ui.kpi_row([
        {"label": "Reports" if admin else "Approved reports", "value": data["reports"], "slot": 0,
         "sub": f"from {data['contributors']} contributor(s)"},
        {"label": "People who voted", "value": votes["voters"], "slot": 1,
         "sub": f"{votes['total']} vote(s) cast"},
        {"label": "Experienced it too", "value": votes["yes"], "slot": 2, "sub": share},
        {"label": "Haven't experienced it", "value": votes["no"], "slot": 3,
         "sub": f"{data['symptoms']} symptom(s) · {data['medications']} medication(s)"},
    ])
    period = (f"{(data['first'] or '')[:10]} to {(data['last'] or '')[:10]}" if data["first"] else "no reports yet")
    ui.note(f"Reporting period {period}. Every figure is a count from people who chose to report or vote - "
            "it is not how often anything happens, and it does not show that a medicine caused it.",
            status="warning", label="Read with care.")

    talk = data.get("discussion") or {}
    if talk:
        ui.kpi_row([
            {"label": "Comments", "value": talk.get("comments", 0), "slot": 4,
             "sub": f"from {talk.get('commenters', 0)} people"},
            {"label": "Likes on comments", "value": talk.get("likes", 0), "slot": 5,
             "sub": "people agreeing with what others shared"},
        ], per_row=2)

    ui.section("Most confirmed by the community", "votes on each aggregated entry")
    _confirmed(data["most_confirmed"], key=f"{key}-confirmed")

    left, right = st.columns(2, gap="large")
    with left:
        ui.section("Most reported symptoms")
        _bar(data["top_symptoms"], "symptom", "reports", key=f"{key}-sym", slot=0)
        ui.section("Severity, as reported")
        _bar(data["severity_mix"], "severity", "reports", key=f"{key}-sev", slot=3,
             order=list(service.SEVERITY_CHOICES), horizontal=False)
        ui.section("Once or repeatedly")
        _bar(data["frequency_mix"], "frequency", "reports", key=f"{key}-freq", slot=5,
             order=list(service.FREQUENCY_CHOICES), horizontal=False)
    with right:
        ui.section("Most reported medications")
        _bar(data["top_medications"], "medication", "reports", key=f"{key}-med", slot=1)
        ui.section("Time to onset")
        onset = [dict(r, onset=service.ONSET_CHOICES.get(r["onset"], r["onset"])) for r in data["onset_mix"]]
        _bar(onset, "onset", "reports", key=f"{key}-onset", slot=4,
             order=list(service.ONSET_CHOICES.values()))
        ui.section("Report types")
        types = [dict(r, type=TYPE_LABELS.get(r["type"], r["type"])) for r in data["type_mix"]]
        _bar(types, "type", "reports", key=f"{key}-type", slot=6, horizontal=False)

    if data["top_foods"]:
        ui.section("Foods and drinks in reports")
        _bar(data["top_foods"], "food", "reports", key=f"{key}-food", slot=2)

    ui.section("Activity by week", "reports submitted and votes cast")
    _weekly(data, key=f"{key}-weekly")


def insights_page() -> None:
    account.sidebar_status()
    ui.masthead(meta=[])
    st.markdown(
        "<div class='ds-eyebrow'>Community insights</div>"
        "<div class='ds-headline'>What people are reporting</div>"
        "<div class='ds-subhead'>Approved reports and community votes, explored. Pick a medication to "
        "focus on it.</div>",
        unsafe_allow_html=True,
    )
    meds = service.medications_with_reports()
    options = [None] + [m["id"] for m in meds]
    chosen = st.selectbox("Medication", options, key="ins-med",
                          format_func=lambda i: "All medications" if i is None
                          else next(m["name"] for m in meds if m["id"] == i))
    data = service.insights(scope="public", medication_id=chosen)
    if not data["reports"]:
        ui.note("No approved reports yet - insights appear as soon as the first reports are reviewed.",
                status="good", label="Nothing to show yet.")
        nav.link("report", "➕ Report a side effect", ":material/add_circle:")
        return
    render(data, key="pub")
