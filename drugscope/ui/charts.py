"""Charts.

Every chart here follows the same rules, and they are not stylistic preferences:

* **The form follows the data's job.** Magnitude over a category gets a bar;
  ordered stages get an ordinal ramp; polarity gets a diverging pair; a single
  headline number is a stat tile in `components.py`, never a gauge.
* **One axis, always.** No chart in this file has two y-scales. Two measures of
  different scale become two charts.
* **Colour follows the entity, not its rank.** Filtering the comparison down from
  three options to two does not repaint the survivors, because slots are assigned
  from a stable entity order.
* **Direct value labels on every bar, plus a table view underneath.** Partly because
  reading a value off a bar is guesswork, and partly because the validated palette
  has slots below 3:1 contrast on the light surface - the relief rule obliges
  visible labels and a table wherever they are used. `chart_with_table` is how that
  obligation is met, so charts go through it rather than straight to
  `st.plotly_chart`.
* **A legend whenever there are two or more series**, and direct labels as well, so
  identity is never carried by colour alone.
"""

from __future__ import annotations

from typing import Any, Sequence

import importlib

import plotly.graph_objects as go
import streamlit as st

from . import theme

class _LazyPandas:
    """pandas, imported when the first chart is drawn rather than at app start."""

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("pandas"), name)


pd = _LazyPandas()

PLOTLY_CONFIG = {
    "displayModeBar": False,
    "scrollZoom": False,
    "responsive": True,
}


def chart_with_table(
    figure: go.Figure,
    table: pd.DataFrame,
    *,
    key: str,
    caption: str = "",
    table_label: str = "Table view",
) -> None:
    """Render a chart with its data available as a table.

    Required, not optional: it satisfies the accessibility pass (a table view always
    exists) and the relief rule for the low-contrast palette slots.
    """
    st.plotly_chart(figure, width="stretch", config=PLOTLY_CONFIG, key=key)
    if caption:
        st.caption(caption)
    with st.expander(table_label, expanded=False):
        st.dataframe(table, width="stretch", hide_index=True)


def _empty(message: str) -> None:
    st.markdown(
        f"<div class='ds-note' style='--ds-note-color:{theme.tokens()['border_strong']}'>"
        f"{message}</div>",
        unsafe_allow_html=True,
    )


def _bar_marker(color: str | Sequence[str]) -> dict[str, Any]:
    """Thin marks, 4px rounded data-ends, 1px surface line for the inter-bar gap."""
    t = theme.tokens()
    return {
        "color": color,
        "cornerradius": 4,
        "line": {"color": t["surface"], "width": 1},
    }


# --------------------------------------------------------------------------- #
# Literature
# --------------------------------------------------------------------------- #

def publication_volume(by_year: list[dict[str, Any]], *, key: str) -> None:
    """Change over time, one series - a bar per year with the count on it."""
    rows = [r for r in by_year if r.get("year")]
    if not rows:
        _empty("No dated publications in the retrieved corpus.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(rows).rename(columns={"year": "Year", "papers": "Papers"})

    figure = go.Figure(go.Bar(
        x=frame["Year"],
        y=frame["Papers"],
        marker=_bar_marker(t["series"][0]),
        text=[str(v) if v else "" for v in frame["Papers"]],
        textposition="outside",
        textfont={"size": 10, "color": t["text_muted"]},
        cliponaxis=False,
        hovertemplate="<b>%{x}</b><br>%{y} paper(s)<extra></extra>",
        name="Papers",
    ))
    layout = theme.plotly_layout(248)
    layout["yaxis"]["title"] = {"text": "Papers retrieved", "font": layout["yaxis"]["title"]["font"]}
    layout["xaxis"]["dtick"] = 1 if len(frame) <= 14 else 2
    figure.update_layout(**layout)

    chart_with_table(
        figure, frame, key=key,
        caption="Retrieved publications by year - reflects this corpus, not total literature volume.",
    )


def evidence_tiers(tier_mix: list[dict[str, Any]], *, key: str) -> None:
    """Ordered categories - an ordinal ramp, strongest tier darkest."""
    rows = [r for r in tier_mix if r.get("count")]
    if not rows:
        _empty("No tiered evidence to display.")
        return

    from ..models import TIER_MEANING

    t = theme.tokens()
    frame = pd.DataFrame(rows).rename(columns={"tier": "Tier", "count": "Records"})
    frame["Means"] = frame["Tier"].map(TIER_MEANING)
    # Ordinal ramp indexed by tier strength, so Tier 1 is always the darkest step.
    ramp = theme.ordinal_scale(4)
    order = {"Tier 1": 3, "Tier 2": 2, "Tier 3": 1, "Tier 4": 0}
    colors = [ramp[order.get(tier, 0)] for tier in frame["Tier"]]

    figure = go.Figure(go.Bar(
        y=frame["Tier"], x=frame["Records"], orientation="h",
        marker=_bar_marker(colors),
        text=frame["Records"], textposition="outside",
        textfont={"size": 11, "color": t["text_muted"]},
        cliponaxis=False,
        customdata=frame["Means"],
        hovertemplate="<b>%{y}</b><br>%{x} record(s)<br>%{customdata}<extra></extra>",
        name="Records",
    ))
    layout = theme.plotly_layout(max(150, 46 * len(frame)))
    layout["yaxis"]["autorange"] = "reversed"
    figure.update_layout(**layout)

    chart_with_table(
        figure, frame, key=key,
        caption="Tier 1 = synthesised or randomised human evidence; Tier 4 = preclinical only.",
    )


def design_mix(designs: list[dict[str, Any]], *, key: str) -> None:
    rows = [r for r in designs if r.get("count")]
    if not rows:
        _empty("No study designs classified.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(rows).rename(columns={"design": "Design", "count": "Records"})

    figure = go.Figure(go.Bar(
        y=frame["Design"], x=frame["Records"], orientation="h",
        marker=_bar_marker(t["series"][0]),
        text=frame["Records"], textposition="outside",
        textfont={"size": 11, "color": t["text_muted"]},
        cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>%{x} record(s)<extra></extra>",
        name="Records",
    ))
    layout = theme.plotly_layout(max(170, 34 * len(frame)))
    layout["yaxis"]["autorange"] = "reversed"
    figure.update_layout(**layout)
    chart_with_table(figure, frame, key=key)


# --------------------------------------------------------------------------- #
# Trials
# --------------------------------------------------------------------------- #

def trial_phases(phase_mix: list[dict[str, Any]], *, key: str) -> None:
    """Development stages are ordered, so the ramp encodes progression."""
    rows = [r for r in phase_mix if r.get("count")]
    if not rows:
        _empty("No phase information in the retrieved trials.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(rows).rename(columns={"phase": "Phase", "count": "Trials"})
    ramp = theme.ordinal_scale(max(len(frame), 2))

    figure = go.Figure(go.Bar(
        x=frame["Phase"], y=frame["Trials"],
        marker=_bar_marker(list(ramp[: len(frame)])),
        text=frame["Trials"], textposition="outside",
        textfont={"size": 11, "color": t["text_muted"]},
        cliponaxis=False,
        hovertemplate="<b>%{x}</b><br>%{y} trial(s)<extra></extra>",
        name="Trials",
    ))
    layout = theme.plotly_layout(250)
    layout["yaxis"]["title"] = {"text": "Trials", "font": layout["yaxis"]["title"]["font"]}
    figure.update_layout(**layout)

    chart_with_table(
        figure, frame, key=key,
        caption="Darker steps are later phases. Counts are retrieved trials, not the full registry.",
    )


def trial_status(status_mix: list[dict[str, Any]], *, key: str) -> None:
    """Status is identity on the category axis, so one hue is enough."""
    rows = [r for r in status_mix if r.get("count")]
    if not rows:
        _empty("No trial statuses to display.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(rows).rename(columns={"status": "Status", "count": "Trials"})

    figure = go.Figure(go.Bar(
        y=frame["Status"], x=frame["Trials"], orientation="h",
        marker=_bar_marker(t["series"][0]),
        text=frame["Trials"], textposition="outside",
        textfont={"size": 11, "color": t["text_muted"]},
        cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>%{x} trial(s)<extra></extra>",
        name="Trials",
    ))
    layout = theme.plotly_layout(max(160, 36 * len(frame)))
    layout["yaxis"]["autorange"] = "reversed"
    figure.update_layout(**layout)
    chart_with_table(figure, frame, key=key)


def trials_over_time(by_start_year: list[dict[str, Any]], *, key: str) -> None:
    rows = [r for r in by_start_year if r.get("year")]
    if len(rows) < 2:
        _empty("Not enough dated trials to plot a trend.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(rows).rename(columns={"year": "Start year", "trials": "Trials"})

    figure = go.Figure(go.Scatter(
        x=frame["Start year"], y=frame["Trials"],
        mode="lines+markers",
        line={"color": t["series"][0], "width": 2, "shape": "spline", "smoothing": 0.5},
        marker={"size": 8, "color": t["series"][0],
                "line": {"color": t["surface"], "width": 2}},
        hovertemplate="<b>%{x}</b><br>%{y} trial(s) started<extra></extra>",
        name="Trials started",
    ))
    layout = theme.plotly_layout(230)
    layout["hovermode"] = "x unified"
    layout["yaxis"]["title"] = {"text": "Trials started", "font": layout["yaxis"]["title"]["font"]}
    figure.update_layout(**layout)

    chart_with_table(
        figure, frame, key=key,
        caption="Trial starts by year - a proxy for research momentum on this subject.",
    )


# --------------------------------------------------------------------------- #
# Claims and evidence structure
# --------------------------------------------------------------------------- #

def claim_direction(direction_mix: list[dict[str, Any]], *, key: str) -> None:
    """Polarity - the diverging pair, with a neutral grey midpoint."""
    rows = [r for r in direction_mix if r.get("count")]
    if not rows:
        _empty("No directional claims extracted.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(rows).rename(columns={"direction": "Direction", "count": "Claims"})
    polarity = {
        "Supports": t["diverging_low"],
        "Refutes": t["diverging_high"],
        "Neutral": t["diverging_mid"],
        "Mixed": t["text_muted"],
    }
    colors = [polarity.get(d, t["text_muted"]) for d in frame["Direction"]]

    figure = go.Figure(go.Bar(
        x=frame["Direction"], y=frame["Claims"],
        marker=_bar_marker(colors),
        text=frame["Claims"], textposition="outside",
        textfont={"size": 11, "color": t["text_muted"]},
        cliponaxis=False,
        hovertemplate="<b>%{x}</b><br>%{y} claim(s)<extra></extra>",
        name="Claims",
    ))
    layout = theme.plotly_layout(230)
    layout["yaxis"]["title"] = {"text": "Claims", "font": layout["yaxis"]["title"]["font"]}
    figure.update_layout(**layout)

    chart_with_table(
        figure, frame, key=key,
        caption="Direction is relative to the intervention being beneficial for that claim's topic.",
    )


def dimension_mix(dimensions: list[dict[str, Any]], *, key: str) -> None:
    rows = [r for r in dimensions if r.get("count")]
    if not rows:
        _empty("No claim dimensions to display.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(rows).rename(columns={"dimension": "Dimension", "count": "Claims"})

    figure = go.Figure(go.Bar(
        y=frame["Dimension"], x=frame["Claims"], orientation="h",
        marker=_bar_marker(t["series"][0]),
        text=frame["Claims"], textposition="outside",
        textfont={"size": 11, "color": t["text_muted"]},
        cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>%{x} claim(s)<extra></extra>",
        name="Claims",
    ))
    layout = theme.plotly_layout(max(160, 34 * len(frame)))
    layout["yaxis"]["autorange"] = "reversed"
    figure.update_layout(**layout)
    chart_with_table(figure, frame, key=key)


def coverage_heatmap(cells: list[dict[str, Any]], *, key: str) -> None:
    """Continuous magnitude across two categories - a sequential heatmap.

    The empty cells are the point: this is where gap analysis gets its evidence.
    """
    if not cells:
        _empty("Not enough structured claims to map coverage.")
        return

    from ..models import TIER_ORDER

    t = theme.tokens()
    frame = pd.DataFrame(cells)
    pivot = frame.pivot(index="dimension", columns="tier", values="count").fillna(0)
    pivot = pivot.reindex(columns=[c for c in TIER_ORDER if c in pivot.columns], fill_value=0)

    if pivot.empty or pivot.to_numpy().sum() == 0:
        _empty("No claims were classified into the coverage grid.")
        return

    ramp = t["sequential"]
    steps = len(ramp) - 1
    colorscale = [[i / steps, color] for i, color in enumerate(ramp)]

    values = pivot.to_numpy()
    peak = values.max() or 1

    figure = go.Figure(go.Heatmap(
        z=values,
        x=list(pivot.columns),
        y=list(pivot.index),
        colorscale=colorscale,
        zmin=0,
        showscale=False,
        xgap=2, ygap=2,  # the 2px surface gap between fills
        hovertemplate="<b>%{y}</b> / %{x}<br>%{z} claim(s)<extra></extra>",
        text=[[int(v) if v else "" for v in row] for row in values],
        texttemplate="%{text}",
        textfont={"size": 11},
    ))
    layout = theme.plotly_layout(max(200, 44 * len(pivot.index)))
    layout["xaxis"]["side"] = "top"
    layout["xaxis"]["gridcolor"] = "rgba(0,0,0,0)"
    layout["yaxis"]["gridcolor"] = "rgba(0,0,0,0)"
    figure.update_layout(**layout)

    table = pivot.reset_index().rename(columns={"dimension": "Dimension"})
    chart_with_table(
        figure, table, key=key,
        caption=(
            f"Claims by dimension and evidence tier (darkest = {int(peak)}). "
            "Empty cells are where this corpus has nothing to say."
        ),
    )


def quality_breakdown(quality: dict[str, Any], *, key: str) -> None:
    """Score against ceiling per component - the index, opened up.

    Two stacked segments per row: points earned, and the headroom remaining to that
    component's weight. One axis, and the total reads as the bar width.
    """
    components = quality.get("components") or {}
    if not components:
        _empty("Evidence Quality Index has no components to break down.")
        return

    t = theme.tokens()
    names = list(components.keys())
    earned = [components[n]["points"] for n in names]
    weights = [components[n]["weight"] for n in names]
    headroom = [round(w - e, 1) for w, e in zip(weights, earned)]

    figure = go.Figure()
    figure.add_bar(
        y=names, x=earned, orientation="h",
        marker=_bar_marker(t["series"][0]),
        text=[f"{e:g}/{w:g}" for e, w in zip(earned, weights)],
        textposition="inside", insidetextanchor="end",
        textfont={"size": 10, "color": t["surface"]},
        hovertemplate="<b>%{y}</b><br>%{x} of %{customdata} points<extra></extra>",
        customdata=weights,
        name="Points earned",
    )
    figure.add_bar(
        y=names, x=headroom, orientation="h",
        marker={"color": t["surface_sunken"], "cornerradius": 4,
                "line": {"color": t["surface"], "width": 1}},
        hoverinfo="skip",
        name="Headroom",
    )
    layout = theme.plotly_layout(max(190, 40 * len(names)), showlegend=True)
    layout["barmode"] = "stack"
    layout["yaxis"]["autorange"] = "reversed"
    layout["xaxis"]["title"] = {"text": "Points (weighted)", "font": layout["xaxis"]["title"]["font"]}
    figure.update_layout(**layout)

    table = pd.DataFrame({
        "Component": names,
        "Points earned": earned,
        "Maximum": weights,
        "Normalised": [components[n]["normalised"] for n in names],
    })
    chart_with_table(
        figure, table, key=key,
        caption=f"Evidence Quality Index {quality.get('score', 0)}/100, by component.",
    )


def topic_agreement(rows: list[dict[str, Any]], *, key: str, limit: int = 12) -> None:
    """Agreement per topic, coloured by whether it is contested."""
    if not rows:
        _empty("No topic carries two or more directional claims, so agreement cannot be measured.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(rows[:limit])
    frame["Topic"] = frame["topic"].str.title()
    colors = [
        t["status"]["serious"] if contested else t["series"][0]
        for contested in frame["contested"]
    ]

    figure = go.Figure(go.Bar(
        y=frame["Topic"], x=frame["agreement"], orientation="h",
        marker=_bar_marker(colors),
        text=[f"{v:g}%" for v in frame["agreement"]],
        textposition="outside",
        textfont={"size": 10, "color": t["text_muted"]},
        cliponaxis=False,
        customdata=frame[["supports", "refutes", "claims"]].to_numpy(),
        hovertemplate=(
            "<b>%{y}</b><br>%{x:.0f}% agreement<br>"
            "%{customdata[0]} supporting, %{customdata[1]} refuting"
            "<br>%{customdata[2]} claims total<extra></extra>"
        ),
        name="Agreement",
    ))
    layout = theme.plotly_layout(max(200, 34 * len(frame)))
    layout["yaxis"]["autorange"] = "reversed"
    layout["xaxis"]["range"] = [0, 108]
    layout["xaxis"]["ticksuffix"] = "%"
    figure.update_layout(**layout)

    table = frame[["Topic", "agreement", "supports", "refutes", "mixed", "claims"]].rename(
        columns={
            "agreement": "Agreement %", "supports": "Supporting",
            "refutes": "Refuting", "mixed": "Neutral/mixed", "claims": "Claims",
        }
    )
    chart_with_table(
        figure, table, key=key,
        caption="Amber marks a contested topic - under 70% of directional claims agree.",
    )


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #

def adverse_events(reactions: list[dict[str, Any]], *, key: str, limit: int = 12) -> None:
    if not reactions:
        _empty("No spontaneous adverse-event reports found for this drug.")
        return

    t = theme.tokens()
    frame = pd.DataFrame(reactions[:limit]).rename(
        columns={"term": "Reported term", "count": "Reports"}
    )

    figure = go.Figure(go.Bar(
        y=frame["Reported term"], x=frame["Reports"], orientation="h",
        marker=_bar_marker(t["series"][0]),
        text=[f"{v:,}" for v in frame["Reports"]],
        textposition="outside",
        textfont={"size": 10, "color": t["text_muted"]},
        cliponaxis=False,
        hovertemplate="<b>%{y}</b><br>%{x:,} report(s)<extra></extra>",
        name="Reports",
    ))
    layout = theme.plotly_layout(max(240, 30 * len(frame)))
    layout["yaxis"]["autorange"] = "reversed"
    figure.update_layout(**layout)

    chart_with_table(
        figure, frame, key=key,
        caption=(
            "Reporting volume, not incidence. No exposure denominator, voluntary "
            "reporting, and a report does not establish causation."
        ),
    )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

def comparison_evidence(
    entity_dimensions: dict[str, dict[str, int]],
    *,
    key: str,
) -> None:
    """Evidence volume by dimension, grouped by option.

    Capped at three options because only the first three categorical slots clear
    colour-blind separation for all pairs. A fourth option is folded into the table
    rather than given a fourth hue.
    """
    if len(entity_dimensions) < 2:
        _empty("Fewer than two options have evidence in this corpus, so there is nothing to compare.")
        return

    t = theme.tokens()
    entities = list(entity_dimensions.keys())
    shown = entities[: theme.ALL_PAIRS_SAFE_SLOTS]
    dimensions = sorted(
        {dim for counts in entity_dimensions.values() for dim in counts},
        key=lambda d: -sum(entity_dimensions[e].get(d, 0) for e in shown),
    )
    if not dimensions:
        _empty("No dimensioned claims to compare across options.")
        return

    colors = theme.series_colors(len(shown), all_pairs=True)
    figure = go.Figure()
    for entity, color in zip(shown, colors):
        values = [entity_dimensions[entity].get(dim, 0) for dim in dimensions]
        figure.add_bar(
            x=dimensions, y=values, name=entity,
            marker=_bar_marker(color),
            text=[str(v) if v else "" for v in values],
            textposition="outside",
            textfont={"size": 10, "color": t["text_muted"]},
            cliponaxis=False,
            hovertemplate=f"<b>{entity}</b><br>%{{x}}: %{{y}} claim(s)<extra></extra>",
        )

    layout = theme.plotly_layout(290, showlegend=True)
    layout["barmode"] = "group"
    layout["bargroupgap"] = 0.08
    layout["yaxis"]["title"] = {"text": "Claims in corpus", "font": layout["yaxis"]["title"]["font"]}
    figure.update_layout(**layout)

    table = pd.DataFrame(
        [{"Dimension": dim, **{e: entity_dimensions[e].get(dim, 0) for e in entities}}
         for dim in dimensions]
    )
    caption = "Where the evidence sits for each option - volume, not quality."
    if len(entities) > len(shown):
        caption += f" {len(entities) - len(shown)} further option(s) are in the table view only."
    chart_with_table(figure, table, key=key, caption=caption)
