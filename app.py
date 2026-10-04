"""DrugScope - pharmaceutical research intelligence.

    streamlit run app.py

This module is the front end and nothing else. It owns no analysis: it collects
settings, drives `drugscope.pipeline`, and renders what comes back. Every number it
shows was computed in `analysis/metrics.py`; every claim it shows carries a citation
handle that resolves to a retrieved record.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from drugscope import export, office
from drugscope.config import (
    APP_NAME,
    DEPTH_PROFILES,
    EXTRACTION_MODEL,
    GATEWAY_MODEL_OVERRIDE,
    PROVIDERS,
    SYNTHESIS_MODEL,
    ResearchDepth,
    RunSettings,
    SourceToggles,
    PROVIDER_ENV,
    WORKER_MODEL_CHOICES,
    credentials_present,
    default_provider,
    misspelled_key_hints,
)
from drugscope.providers import GATEWAYS, OPENAI_MODELS, free_model_catalogue
from drugscope.models import DESIGN_LABELS, Report
from drugscope.sources.base import clear_cache
from drugscope import jobs
from drugscope.community import auth, projects, quota, service as community_service
from drugscope.ui import account, admin as admin_ui, charts, community as community_ui, components as ui
from drugscope.ui import home, insights as insights_ui, nav, projects as projects_ui, theme

st.set_page_config(
    page_title=f"{APP_NAME} - Pharmaceutical Research Intelligence",
    page_icon="\N{MICROSCOPE}",
    layout="wide",
    initial_sidebar_state="expanded",
)

EXAMPLES = [
    "Semaglutide for cardiovascular risk reduction in obesity without diabetes",
    "Compare lecanemab and donanemab for early Alzheimer's disease",
    "Current evidence for GLP-1 receptor agonists in chronic kidney disease",
    "Safety profile of JAK inhibitors in rheumatoid arthritis",
    "Emerging treatments for triple-negative breast cancer",
    "Psilocybin for treatment-resistant depression: state of the evidence",
]


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def init_state() -> None:
    defaults: dict[str, Any] = {
        "query_box": "",
        "error": None,
        # Set by the example buttons, applied on the next run - see below.
        "pending_query": None,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def apply_pending_query() -> None:
    """Move a queued example into the search box before the widget is built.

    Streamlit refuses `st.session_state.query_box = ...` once the text area with
    that key has been instantiated in the current run, and the example buttons sit
    *below* the form. So a click stores the text under a separate key and reruns;
    this runs first on that next pass, when `query_box` is still a plain value.
    """
    pending = st.session_state.pop("pending_query", None)
    if pending:
        st.session_state["query_box"] = pending
    depth = st.session_state.pop("pending_depth", None)
    if depth:
        st.session_state["research_depth"] = depth


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #

CUSTOM_MODEL = "Custom…"


def gateway_model_picker(provider: str, profile: Any) -> str:
    """Model choice for an OpenAI-compatible gateway.

    OpenRouter's free list is read live, because its free models are retired every
    few months and a hard-coded list turns into a list of 404s. A model named in
    `.env` is preselected when the gateway still serves it, and called out when it
    does not.
    """
    override = GATEWAY_MODEL_OVERRIDE
    if provider == "openrouter":
        catalogue = free_model_catalogue()
        options = [m["id"] for m in catalogue]
        retired = bool(override) and override.endswith(":free") and override not in options
        if override and not override.endswith(":free") and override not in options:
            options.insert(0, override)  # a paid route chosen in .env
        help_text = (
            f"{len(catalogue)} free models OpenRouter serves right now, best JSON "
            "support first. Choose Custom for a paid model id."
        )
    else:
        options = list(OPENAI_MODELS)
        retired = False
        if override and "/" not in override and override not in options:
            options.insert(0, override)
        help_text = "OpenAI chat models. Choose Custom for any other model id."

    preferred = override if override in options else options[0]
    choice = st.selectbox(
        "Model", options + [CUSTOM_MODEL],
        index=options.index(preferred), help=help_text,
    )
    model = (
        st.text_input("Model id", placeholder="vendor/model-name").strip()
        if choice == CUSTOM_MODEL else choice
    )

    if retired:
        st.caption(
            f"⚠ `{override}` from `.env` is no longer offered by OpenRouter, "
            f"so `{options[0]}` is used instead."
        )
    if model.endswith(":free"):
        # Plan + synthesis + one call per batch of six records.
        calls = 2 + -(-(profile.papers + profile.trials + 10) // 6)
        st.caption(
            f"Free tier: records are appraised in batches and the web sweep is off. "
            f"This depth uses about {calls} requests; free keys allow ~50 a day."
        )
    return model


def sidebar() -> tuple[RunSettings | None, bool]:
    with st.sidebar:
        user = account.current_user()
        if user is not None:
            account.credits_meter(quota.status(user))
        else:
            st.caption("Sign in to run research.")
            nav.link("account", "Sign in or create an account", ":material/login:")
        st.divider()

        depth_label = st.segmented_control(
            "Research depth",
            options=[d.value for d in ResearchDepth],
            default=ResearchDepth.STANDARD.value,
            key="research_depth",
            help="Controls how much evidence is retrieved and how hard the model reasons over it.",
        ) or ResearchDepth.STANDARD.value
        depth = ResearchDepth(depth_label)
        profile = DEPTH_PROFILES[depth]
        cost = quota.DEPTH_COST[depth.value]

        st.caption(
            f"{profile.blurb}  \n"
            f"**{profile.papers}** papers · **{profile.trials}** trials · "
            f"effort `{profile.effort}` · {profile.est_minutes} · "
            f"**{cost}** credit{'s' if cost != 1 else ''}"
        )

        st.divider()
        st.markdown("**Evidence streams**")
        toggles = SourceToggles(
            literature=st.checkbox("Scientific literature", True, help="Europe PMC and PubMed"),
            trials=st.checkbox("Clinical trials", True, help="ClinicalTrials.gov registry"),
            regulatory=st.checkbox("Regulatory and safety", True, help="Drugs@FDA, labels, FAERS, recalls"),
            pharmacology=st.checkbox("Pharmacology", True, help="ChEMBL mechanisms and RxNorm classes"),
            recent_news=st.checkbox(
                "Recent developments (web)", True,
                help="Server-side web search over trusted domains. Off at Scan depth.",
            ),
        )

        st.divider()
        with st.expander("Scope and filters", expanded=False):
            comparator_text = st.text_input(
                "Benchmark against",
                placeholder="tirzepatide, dulaglutide",
                help="Comma-separated. Forces these into the comparison matrix.",
            )
            focus = st.text_area(
                "Narrow the focus",
                placeholder="Patients over 65; cardiovascular endpoints only",
                height=76,
            )
            year_from = st.number_input(
                "Published from year", min_value=1950, max_value=2100, value=2015, step=1,
                help="Applied to the literature search only.",
            )
            use_year = st.checkbox("Apply the year filter", value=False)

        st.divider()

        # Documents - the retrieval-augmented half of the corpus.
        with st.expander("➕ Add your own documents (optional)", expanded=False):
            st.caption(
                "PDF, TXT, MD or CSV. Uploads are chunked, ranked against your "
                "question and cited like any other source."
            )
            uploads = st.file_uploader(
                "Add files",
                type=["pdf", "txt", "md", "csv", "tsv", "json"],
                accept_multiple_files=True,
                label_visibility="collapsed",
            )
            documents = [(f.name, f.getvalue()) for f in (uploads or [])]
            if documents:
                total_kb = sum(len(d) for _, d in documents) / 1024
                st.caption(f"{len(documents)} file(s) attached \u00b7 {total_kb:,.0f} KB")

        # Provider - compact, because it is set once and then forgotten.
        provider_keys = list(PROVIDERS)
        start = default_provider()
        current = st.session_state.get("provider", start)
        current_ok = credentials_present(current)
        with st.expander(
            f"Model \u00b7 {PROVIDERS.get(current, current)}"
            + ("" if current_ok else "  (no key)"),
            expanded=not current_ok,
        ):
            provider = st.radio(
                "Provider",
                options=provider_keys,
                index=provider_keys.index(start),
                key="provider",
                format_func=lambda key: PROVIDERS[key],
                horizontal=True,
                label_visibility="collapsed",
            )

            extraction_model = EXTRACTION_MODEL
            gateway_model = ""

            if provider in GATEWAYS:
                gateway_model = gateway_model_picker(provider, profile)
            else:
                extraction_model = st.selectbox(
                    "Appraisal model", WORKER_MODEL_CHOICES,
                    index=WORKER_MODEL_CHOICES.index(EXTRACTION_MODEL),
                    help=f"Planning and synthesis run on {SYNTHESIS_MODEL}. This is "
                         "the per-record worker - one call per retrieved record.",
                )

            credentials = credentials_present(provider)
            env_var = PROVIDER_ENV.get(provider, "")
            if credentials:
                st.markdown(ui.badge("Key found", status="good"), unsafe_allow_html=True)
            else:
                st.markdown(ui.badge("No key", status="critical"), unsafe_allow_html=True)
                st.caption(f"Add `{env_var}` to your `.env`, then restart the app.")
                for hint in misspelled_key_hints():
                    st.caption(f"\u26a0 {hint}")

            if st.button("Clear retrieval cache", width="stretch"):
                clear_cache()
                free_model_catalogue(refresh=True)
                st.toast("Retrieval cache cleared.")

        projects_ui.sidebar_recent()
    account.workspace_card()

    query = st.session_state.get("query_box", "").strip()
    settings = RunSettings(
        query=query,
        depth=depth,
        toggles=toggles,
        comparators=[c.strip() for c in comparator_text.split(",") if c.strip()],
        focus=focus,
        year_from=int(year_from) if use_year else None,
        extraction_model=extraction_model,
        provider=provider,
        gateway_model=gateway_model,
        documents=documents,
    )
    return settings, credentials


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #

def search_form(*, compact: bool) -> bool:
    """The one live widget on the home screen."""
    depth = st.session_state.get("research_depth") or ResearchDepth.STANDARD.value
    cost = quota.DEPTH_COST.get(depth, 2)
    with st.form("search", clear_on_submit=False):
        st.text_area(
            "Research question",
            key="query_box",
            height=88 if compact else 96,
            placeholder='Search a drug, disease, treatment or study\u2026  '
                        'e.g. "How does semaglutide compare with tirzepatide, '
                        'and what are the open safety questions?"',
            label_visibility="collapsed",
        )
        left, right = st.columns([2.5, 1.5])
        with left:
            st.caption(
                "Europe PMC \u00b7 PubMed \u00b7 ClinicalTrials.gov \u00b7 Drugs@FDA \u00b7 "
                "FDA labels \u00b7 FAERS \u00b7 ChEMBL \u00b7 RxNorm"
            )
        with right:
            submitted = st.form_submit_button(
                f"Run research · {cost} credit{'s' if cost != 1 else ''}", type="primary", width="stretch"
            )
    account.credit_line(cost, depth)
    return submitted


def search_panel(*, credentials: bool, provider: str) -> tuple[bool, Any]:
    """The home screen: hero, search box, capabilities and examples.

    Returns (submitted, run_slot). The run slot sits directly under the search box,
    so a run's progress appears where the user is looking rather than below the
    whole home page.
    """
    main, rail = st.columns([2.1, 1], gap="large")

    with main:
        home.hero()
        if not credentials:
            ui.setup_card(
                provider_label=PROVIDERS.get(provider, provider),
                env_var=PROVIDER_ENV.get(provider, "OPENROUTER_API_KEY"),
                hints=misspelled_key_hints(),
            )
        submitted = search_form(compact=False)
        run_slot = st.container()

        ui.section("What DrugScope does")
        home.capability_grid()

        st.markdown(
            "<div style='font-size:.68rem;font-weight:650;letter-spacing:.09em;"
            "text-transform:uppercase;color:var(--ds-ink-3);margin:1.6rem 0 .5rem'>"
            "Start from an example</div>",
            unsafe_allow_html=True,
        )
        for row in (EXAMPLES[:2], EXAMPLES[2:4], EXAMPLES[4:]):
            columns = st.columns(len(row))
            for column, example in zip(columns, row):
                with column:
                    if st.button(example, key=f"ex-{example[:18]}", width="stretch"):
                        # The search box already exists in this run, so its value
                        # can only be set before it is built - see apply_pending_query.
                        st.session_state.pending_query = example
                        st.rerun()

    with rail:
        home.what_you_get()
        home.how_it_works()
        home.trust_note()

    return submitted, run_slot


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #

def headline_kpis(report: Report) -> None:
    m = report.metrics
    s = report.synthesis.summary
    quality = m["quality"]
    consensus = m["consensus"]
    trials = m["trials"]

    ui.kpi_row([
        {
            "label": "Evidence quality",
            "value": quality["score"], "unit": "/100",
            "slot": 0, "meter": quality["score"] / 100,
            "sub": f"Across {quality.get('basis', 0)} papers and trials",
            "help_text": "Weighted composite: best available design 30, depth of top-tier "
                         "evidence 25, human evidence 15, recency 15, corroboration 15.",
        },
        {
            "label": "Consensus",
            "value": consensus["score"], "unit": "/100",
            "status": "good" if consensus["score"] >= 75 else
                      "warning" if consensus["score"] >= 55 else "serious",
            "meter": consensus["score"] / 100,
            "sub": f"{len(consensus['contested_topics'])} contested of "
                   f"{consensus['topics_assessed']} topics assessed",
            "help_text": "Share of directional claims held by the majority position, "
                         "weighted by claim volume per topic.",
        },
        {
            "label": "Development maturity",
            "value": s.maturity, "unit": "/100",
            "slot": 2, "meter": s.maturity / 100,
            "sub": f"Highest trial phase retrieved: {trials.get('highest_phase', 'n/a')}",
            "help_text": "Bench-to-practice progress judged from trial phases, the "
                         "approval record and guideline presence in the corpus.",
        },
        {
            "label": "Sources analysed",
            "value": m["totals"]["records"],
            "slot": 6,
            "sub": f"{m['totals']['claims']} structured claims · {m['year_span']}",
            "help_text": "Every source is citable and listed in the Sources tab.",
        },
    ])


def tab_overview(report: Report, records: dict[str, Any]) -> None:
    s = report.synthesis
    m = report.metrics

    ui.verdict(
        s.summary.verdict,
        confidence=s.summary.confidence,
        rationale=s.summary.confidence_rationale,
    )

    left, right = st.columns([1.55, 1], gap="large")

    with left:
        ui.section("Executive summary")
        ui.prose(s.summary.narrative, records)

        if s.summary.watch_items:
            ui.section("What to watch", "developments that would change this picture")
            for item in s.summary.watch_items:
                st.markdown(
                    f"<div style='font-size:.85rem;color:var(--ds-ink-2);padding:.3rem 0 .3rem .8rem;"
                    f"border-left:2px solid var(--ds-border-strong);margin-bottom:.3rem'>{ui.esc(item)}</div>",
                    unsafe_allow_html=True,
                )

        briefing = m.get("recent_developments", "")
        if briefing and not briefing.startswith("("):
            ui.section("Recent developments", "open-web sweep - the weakest evidence tier")
            ui.prose(briefing, records)

    with right:
        ui.section("Evidence quality, decomposed")
        charts.quality_breakdown(m["quality"], key="ov-quality")

        ui.section("Claim direction")
        charts.claim_direction(m["claims"]["direction_mix"], key="ov-direction")

    numbers = m.get("number_check") or {}
    checked = numbers.get("checked", 0)
    note = (f"{checked - numbers.get('unverified', 0)} of {checked} figures match their cited sources"
            if checked else "ordered by decision relevance")
    ui.section("Key findings", f"{len(s.key_findings)} findings · {note}")
    for i, finding in enumerate(s.key_findings, 1):
        ui.finding_card(finding, records, index=i, numbers=(numbers.get("findings") or {}).get(i))


def tab_evidence(report: Report) -> None:
    m = report.metrics
    lit = m["literature"]

    ui.kpi_row([
        {"label": "Papers appraised", "value": lit["count"], "slot": 0,
         "sub": f"{lit['recent_share']}% from the last three years"},
        {"label": "Randomised or synthesised", "value": sum(
            row["count"] for row in lit["tier_mix"] if row["tier"] == "Tier 1"
        ), "slot": 2, "sub": "Tier 1 records in the corpus"},
        {"label": "Total citations", "value": f"{lit['total_citations']:,}", "slot": 6,
         "sub": "Europe PMC citation counts, where available"},
        {"label": "Quantified claims", "value": m["claims"].get("quantified_share", 0),
         "unit": "%", "slot": 4, "meter": m["claims"].get("quantified_share", 0) / 100,
         "sub": "Claims carrying an explicit effect size"},
    ])

    left, right = st.columns(2, gap="large")
    with left:
        ui.section("Publication volume")
        charts.publication_volume(lit["by_year"], key="ev-years")
        ui.section("Evidence tiers")
        charts.evidence_tiers(lit["tier_mix"], key="ev-tiers")
    with right:
        ui.section("Study designs")
        charts.design_mix(lit["design_mix"], key="ev-designs")
        ui.section("Claim dimensions")
        charts.dimension_mix(m["claims"]["dimension_mix"], key="ev-dims")

    ui.section("Coverage map", "where this corpus has evidence, and where it does not")
    charts.coverage_heatmap(m["coverage"]["cells"], key="ev-coverage")

    if lit["top_journals"]:
        ui.section("Where this evidence was published")
        st.dataframe(
            [{"Journal": row["journal"], "Papers": row["count"]} for row in lit["top_journals"]],
            width="stretch", hide_index=True,
        )


def tab_trials(report: Report) -> None:
    m = report.metrics
    trials = m["trials"]

    if not trials.get("count"):
        ui.note("No clinical trials were retrieved for this question.", status="warning")
        return

    largest = trials.get("largest_trial") or {}
    ui.kpi_row([
        {"label": "Trials retrieved", "value": trials["count"], "slot": 0,
         "sub": f"of {trials.get('registry_total', 0):,} matching the registry search"},
        {"label": "Active", "value": trials["active"], "status": "good",
         "sub": f"{trials['completed']} completed · {trials['stopped']} stopped"},
        {"label": "Highest phase", "value": trials["highest_phase"], "slot": 2,
         "sub": f"{trials['with_results']} trials have posted results"},
        {"label": "Participants", "value": f"{trials['total_enrollment']:,}", "slot": 6,
         "sub": f"median {trials['median_enrollment']:,} per trial · "
                f"{trials['industry_share']}% industry-sponsored"},
    ])

    left, right = st.columns(2, gap="large")
    with left:
        ui.section("Phase distribution")
        charts.trial_phases(trials["phase_mix"], key="tr-phases")
    with right:
        ui.section("Recruitment status")
        charts.trial_status(trials["status_mix"], key="tr-status")

    ui.section("Research momentum", "trial starts by year")
    charts.trials_over_time(trials["by_start_year"], key="tr-time")

    if largest.get("enrollment"):
        ui.note(
            f"Largest retrieved trial: {largest['title']} ({largest['nct']}), "
            f"{largest['phase']}, n={largest['enrollment']:,}.",
            status="good", label="Anchor study.",
        )

    ui.section("Trial register")
    rows = []
    for record in report.records:
        if record.kind != "trial":
            continue
        meta = record.meta
        rows.append({
            "Handle": record.sid,
            "NCT": record.identifiers.get("nct", ""),
            "Phase": meta.get("phase", ""),
            "Status": meta.get("status", ""),
            "Enrolment": meta.get("enrollment") or None,
            "Started": meta.get("start_date", ""),
            "Sponsor": meta.get("sponsor", ""),
            "Results": "Yes" if meta.get("has_results") else "No",
            "Title": record.title,
        })
    st.dataframe(rows, width="stretch", hide_index=True)


def tab_regulatory(report: Report, records: dict[str, Any]) -> None:
    m = report.metrics
    reg = m["regulatory"]
    faers = reg.get("faers") or {}
    s = report.synthesis

    ui.kpi_row([
        {"label": "FDA applications", "value": len(reg.get("applications") or []), "slot": 0,
         "sub": ", ".join(reg.get("applications") or []) or "none found"},
        {"label": "Approval actions", "value": reg.get("approval_actions", 0), "slot": 2,
         "sub": f"first {reg.get('first_approval') or 'n/a'} · "
                f"latest {reg.get('latest_approval') or 'n/a'}"},
        {"label": "Boxed warning",
         "value": "Yes" if reg.get("has_boxed_warning") else "Not found",
         "status": "critical" if reg.get("has_boxed_warning") else "good",
         "sub": "From the approved US prescribing information"},
        {"label": "Recall records", "value": reg.get("recalls", 0),
         "status": "serious" if reg.get("critical_recalls") else "warning",
         "sub": f"{reg.get('critical_recalls', 0)} Class I (most serious)"},
    ])

    ui.section("Regulatory status")
    ui.prose(s.regulatory_status, records)

    if reg.get("boxed_warning_text"):
        ui.note(reg["boxed_warning_text"][:900], status="critical", label="Boxed warning.")

    ui.section("Safety signals", "from labels, trials and post-market reporting")
    if s.safety_signals:
        for signal in s.safety_signals:
            ui.safety_card(signal, records)
    else:
        ui.note("No discrete safety signals were extracted from this corpus.", status="warning")

    if faers.get("top_reactions"):
        ui.section("Spontaneous adverse-event reports", "openFDA FAERS")
        ui.note(faers.get("caveat", ""), status="warning", label="Read this first.")

        ui.kpi_row([
            {"label": "Reports flagged serious", "value": f"{faers.get('serious_reports', 0):,}",
             "status": "serious", "sub": "Count of reports, not patients or incidence"},
            {"label": "Distinct terms shown", "value": len(faers["top_reactions"]), "slot": 6,
             "sub": "Most-reported MedDRA preferred terms"},
        ], per_row=2)

        charts.adverse_events(faers["top_reactions"], key="rg-faers")

        if faers.get("outcome_mix"):
            with st.expander("Reported outcome mix", expanded=False):
                st.dataframe(
                    [{"Outcome": row["label"], "Reports": row["count"]}
                     for row in faers["outcome_mix"]],
                    width="stretch", hide_index=True,
                )

    ui.section("Mechanism")
    ui.prose(s.mechanism, records)


def tab_timeline(report: Report, records: dict[str, Any]) -> None:
    events = report.synthesis.timeline
    ui.section(
        "Development timeline",
        f"{len(events)} dated events, each drawn from a retrieved record",
    )
    ui.timeline(events, records)


def tab_comparison(report: Report, records: dict[str, Any]) -> None:
    comparison = report.synthesis.comparison

    if not comparison.entities or not comparison.rows:
        ui.note(
            comparison.bottom_line
            or "The retrieved corpus does not support a head-to-head comparison. Add "
               "comparators in the sidebar to force a benchmark.",
            status="warning",
            label="No comparison built.",
        )
        return

    ui.section("Head-to-head", f"{len(comparison.entities)} options across {len(comparison.rows)} criteria")

    header = st.columns([1.25] + [1.6] * len(comparison.entities) + [1.4])
    header[0].markdown("<div style='font-size:.7rem;font-weight:650;letter-spacing:.08em;"
                       "text-transform:uppercase;color:var(--ds-ink-3)'>Criterion</div>",
                       unsafe_allow_html=True)
    for column, entity in zip(header[1:], comparison.entities):
        column.markdown(
            f"<div style='font-size:.85rem;font-weight:650;color:var(--ds-ink)'>{ui.esc(entity)}</div>",
            unsafe_allow_html=True,
        )
    header[-1].markdown("<div style='font-size:.7rem;font-weight:650;letter-spacing:.08em;"
                        "text-transform:uppercase;color:var(--ds-ink-3)'>Verdict</div>",
                        unsafe_allow_html=True)
    st.divider()

    for row in comparison.rows:
        columns = st.columns([1.25] + [1.6] * len(comparison.entities) + [1.4])
        columns[0].markdown(
            f"<div style='font-size:.82rem;font-weight:600;color:var(--ds-ink)'>"
            f"{ui.esc(row.criterion)}</div>", unsafe_allow_html=True,
        )
        by_entity = {cell.entity: cell for cell in row.cells}
        for column, entity in zip(columns[1:-1], comparison.entities):
            cell = by_entity.get(entity)
            if cell is None:
                column.markdown(
                    "<div style='font-size:.79rem;color:var(--ds-ink-3)'>Not established</div>",
                    unsafe_allow_html=True,
                )
                continue
            column.markdown(
                f"<div style='font-size:.79rem;color:var(--ds-ink-2);line-height:1.45'>"
                f"{ui.esc(cell.value)}</div>"
                f"<div style='margin-top:.25rem'>"
                f"{ui.citations_markup(cell.citations, records)}</div>",
                unsafe_allow_html=True,
            )
        columns[-1].markdown(
            f"<div style='font-size:.78rem;color:var(--ds-ink);font-weight:520'>"
            f"{ui.esc(row.verdict) if row.verdict else '&mdash;'}</div>", unsafe_allow_html=True,
        )
        st.markdown(
            "<div style='border-bottom:1px solid var(--ds-border);margin:.35rem 0 .55rem'></div>",
            unsafe_allow_html=True,
        )

    ui.note(comparison.bottom_line, status="good", label="Where the evidence points.")

    # Evidence volume per option, from the claims actually extracted.
    entity_dimensions: dict[str, dict[str, int]] = {}
    for entity in comparison.entities:
        needle = entity.lower()
        counts: dict[str, int] = {}
        for sid, appraisal in report.appraisals.items():
            record = report.record_by_sid(sid)
            haystack = f"{record.title} {record.snippet}".lower() if record else ""
            for claim in appraisal.claims:
                if needle in haystack or needle in claim.statement.lower():
                    label = claim.dimension.title()
                    counts[label] = counts.get(label, 0) + 1
        if counts:
            entity_dimensions[entity] = counts

    if entity_dimensions:
        ui.section("Evidence base by option", "claim volume, not claim quality")
        charts.comparison_evidence(entity_dimensions, key="cmp-evidence")


def tab_conflicts(report: Report, records: dict[str, Any]) -> None:
    conflicts = report.synthesis.conflicts
    consensus = report.metrics["consensus"]

    ui.kpi_row([
        {"label": "Consensus index", "value": consensus["score"], "unit": "/100",
         "slot": 0, "meter": consensus["score"] / 100,
         "sub": f"across {consensus['topics_assessed']} topics with directional claims"},
        {"label": "Contested topics", "value": len(consensus["contested_topics"]),
         "status": "serious" if consensus["contested_topics"] else "good",
         "sub": "under 70% agreement among directional claims"},
        {"label": "Adjudicated conflicts", "value": len(conflicts),
         "status": "critical" if any(c.severity == "decision_changing" for c in conflicts) else "warning",
         "sub": f"{sum(1 for c in conflicts if c.severity == 'decision_changing')} "
                "could change a decision"},
    ], per_row=3)

    ui.section("Agreement by topic", "computed from extracted claims, before any adjudication")
    charts.topic_agreement(consensus["rows"], key="cf-agreement")

    ui.section("Adjudicated conflicts")
    if conflicts:
        for conflict in conflicts:
            ui.conflict_card(conflict, records)
    else:
        ui.note(
            "No direct contradictions were identified. Internal consistency is not the "
            "same as correctness - it can also mean the corpus is too small or too "
            "homogeneous to disagree with itself.",
            status="warning", label="No conflicts found.",
        )


def tab_gaps(report: Report) -> None:
    gaps = report.synthesis.gaps
    thin = report.metrics["coverage"]["thin_dimensions"]

    ui.kpi_row([
        {"label": "Open questions", "value": len(gaps), "slot": 0,
         "sub": f"{sum(1 for g in gaps if g.priority == 'critical')} critical"},
        {"label": "Under-evidenced dimensions", "value": len(thin),
         "status": "serious" if thin else "good",
         "sub": ", ".join(row["dimension"] for row in thin) or "none - all dimensions covered"},
        {"label": "Mean source relevance", "value": report.metrics["mean_relevance"],
         "unit": "/100", "slot": 2, "meter": report.metrics["mean_relevance"] / 100,
         "sub": "How directly the corpus addresses the question"},
    ], per_row=3)

    ui.section("Research gaps", "ordered by priority")
    if gaps:
        priority_order = {"critical": 0, "high": 1, "moderate": 2}
        for i, gap in enumerate(sorted(gaps, key=lambda g: priority_order.get(g.priority, 9)), 1):
            ui.gap_card(gap, index=i)
    else:
        ui.note("No research gaps were identified.", status="warning")

    if report.plan.open_questions:
        ui.section("Questions raised at planning", "before any evidence was retrieved")
        for question in report.plan.open_questions:
            st.markdown(
                f"<div style='font-size:.84rem;color:var(--ds-ink-2);padding:.3rem 0 .3rem .8rem;"
                f"border-left:2px solid var(--ds-border-strong);margin-bottom:.3rem'>"
                f"{ui.esc(question)}</div>", unsafe_allow_html=True,
            )


def tab_sources(report: Report) -> None:
    kinds = {
        "paper": "Literature",
        "trial": "Clinical trials",
        "regulatory": "Regulatory",
        "safety": "Recalls",
        "compound": "Pharmacology",
        "news": "Web",
    }
    present = [k for k in kinds if any(r.kind == k for r in report.records)]

    ui.section("Source register", f"{len(report.records)} records, all citable")
    chosen = st.pills(
        "Filter", ["All"] + [kinds[k] for k in present], default="All",
        label_visibility="collapsed",
    ) or "All"

    label_to_kind = {v: k for k, v in kinds.items()}
    wanted = None if chosen == "All" else label_to_kind.get(chosen)

    for record in report.records:
        if wanted is None or record.kind == wanted:
            ui.source_row(record)

    with st.expander("Per-record appraisals", expanded=False):
        rows = []
        for sid, appraisal in report.appraisals.items():
            record = report.record_by_sid(sid)
            rows.append({
                "Handle": sid,
                "Relevance": int(appraisal.relevance),
                "Design": DESIGN_LABELS.get(appraisal.design, appraisal.design),
                "n": appraisal.sample_size if appraisal.sample_size > 0 else None,
                "Claims": len(appraisal.claims),
                "Summary": appraisal.summary,
                "Limitations": "; ".join(appraisal.limitations[:2]),
                "Title": record.title if record else "",
            })
        rows.sort(key=lambda r: -r["Relevance"])
        st.dataframe(rows, width="stretch", hide_index=True)


def tab_export(report: Report) -> None:
    stem = export.filename_stem(report)
    markdown = export.to_markdown(report)

    ui.section("Export", "every format carries the full reference list and the methodology note")
    left, middle, right = st.columns(3)
    left.download_button(
        "Markdown report", markdown, file_name=f"{stem}.md",
        mime="text/markdown", width="stretch", type="primary",
    )
    middle.download_button(
        "Structured JSON", export.to_json(report), file_name=f"{stem}.json",
        mime="application/json", width="stretch",
    )
    right.download_button(
        "Source register (CSV)", export.to_csv(report), file_name=f"{stem}.csv",
        mime="text/csv", width="stretch",
    )

    ui.section("Presentation and document", "ready to share - built from this report, same citations")
    deck_col, doc_col = st.columns(2)
    deck_col.download_button(
        "\U0001F4CA PowerPoint presentation (.pptx)", office.to_pptx(report), file_name=f"{stem}.pptx",
        mime="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        width="stretch", type="primary",
    )
    doc_col.download_button(
        "\U0001F4C4 Word report (.docx)", office.to_docx(report), file_name=f"{stem}.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        width="stretch", type="primary",
    )

    ui.section("Methodology and limitations")
    st.markdown("\n".join(export._methodology(report)))

    if report.warnings:
        ui.section("Run diagnostics", f"{len(report.warnings)} notices")
        for warning in report.warnings:
            st.markdown(
                f"<div style='font-size:.78rem;color:var(--ds-ink-3);font-family:var(--ds-mono);"
                f"padding:.18rem 0'>{ui.esc(warning)}</div>", unsafe_allow_html=True,
            )

    with st.expander("Preview the markdown report", expanded=False):
        st.code(markdown, language="markdown")


def render_report(report: Report) -> None:
    records = {r.sid: r for r in report.records}

    st.markdown(
        f"<div style='font-size:1.3rem;font-weight:640;letter-spacing:-.022em;"
        f"line-height:1.3;margin:.4rem 0 .2rem'>{ui.esc(report.query)}</div>"
        f"<div style='font-size:.8rem;color:var(--ds-ink-3);margin-bottom:1.1rem'>"
        f"{ui.esc(report.plan.interpretation)}</div>",
        unsafe_allow_html=True,
    )

    headline_kpis(report)

    if report.plan.disclaimers:
        ui.note(" ".join(report.plan.disclaimers), status="warning", label="Scope note.")

    sections = [
        ("Overview", lambda: tab_overview(report, records)),
        ("Evidence", lambda: tab_evidence(report)),
        ("Trials", lambda: tab_trials(report)),
        ("Regulatory & safety", lambda: tab_regulatory(report, records)),
        ("Timeline", lambda: tab_timeline(report, records)),
        ("Comparison", lambda: tab_comparison(report, records)),
        ("Conflicts", lambda: tab_conflicts(report, records)),
        ("Gaps", lambda: tab_gaps(report)),
        ("Sources", lambda: tab_sources(report)),
        ("Export", lambda: tab_export(report)),
    ]
    # Lazy tabs: only the open tab is built. Rendering all ten - fifteen-odd charts -
    # on every click is what made the report view sluggish.
    tabs = st.tabs([name for name, _ in sections], key="report_tab", on_change="rerun")
    for tab, (_, render) in zip(tabs, sections):
        if tab.open:
            with tab:
                render()

    ui.spend_line(report.usage, report.elapsed_seconds, depth=report.depth)
    st.caption(
        "DrugScope is research intelligence for qualified professionals. It is not "
        "medical advice and not a substitute for the approved product label or a "
        "prescriber's judgement."
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _short_model(settings: RunSettings) -> str:
    name = settings.gateway_model if settings.provider in GATEWAYS else SYNTHESIS_MODEL
    name = (name or "default").split("/")[-1].removesuffix(":free")
    return name if len(name) <= 26 else name[:25] + "…"


def run_with_quota(settings: RunSettings) -> None:
    """Charge credits, create the project, and start the research in the background.

    The run gets its own project page right away - that is where its progress, its
    report and its assistant live. The background job refunds the credits itself if
    the run fails.
    """
    user = account.current_user()
    if user is None:
        ui.note(
            f"Create a free account to run research - it includes "
            f"{quota.plans()['free'].credits} credits every {quota.window_hours()} hours.",
            status="warning", label="Sign in required.",
        )
        nav.link("account", "Sign in or create an account", ":material/login:")
        return

    cost = quota.DEPTH_COST[settings.depth.value]
    try:
        event_id = quota.consume(user, cost, kind="research",
                                 detail=f"{settings.depth.value}: {settings.query[:200]}")
    except quota.QuotaExceeded as exc:
        wait = exc.status.resets_in_text()
        ui.note(
            f"{exc} Credits come back as earlier runs leave the {exc.status.window_hours}-hour window"
            + (f" - the next one returns in {wait}." if wait else "."),
            status="critical", label="Out of research credits.",
        )
        if exc.status.remaining >= quota.DEPTH_COST["Scan"]:
            st.caption("A Scan (1 credit) still fits - pick it under Research depth.")
        account.upgrade_prompt(exc.status, key="research-upgrade")
        return

    try:
        project_id = projects.create(user, settings.query, settings.depth.value)
        jobs.start(project_id, settings, user, event_id)
    except Exception:
        quota.refund(user, event_id)
        raise
    status = quota.status(user)
    st.session_state["toast"] = (
        "Research started - administrators are never charged." if status.unlimited else
        f"Research started - used {cost} credit{'s' if cost != 1 else ''}, {status.remaining} of "
        f"{status.limit} left. Refunded automatically if it fails."
    )
    projects_ui.open_project(project_id)
    st.rerun()


def restart_research(query: str, depth: str) -> None:
    """Run a failed project's question again, as a new project."""
    # Widget values can only be set before the widgets are drawn - see apply_pending_query.
    st.session_state["pending_query"] = query
    st.session_state["pending_depth"] = depth
    st.session_state["autostart"] = True
    projects_ui.close_project()
    st.rerun()


def research_page() -> None:
    init_state()
    apply_pending_query()

    # The sidebar renders into its own container, so building it first does not
    # move it - it just lets the masthead describe the active configuration.
    settings, credentials = sidebar()

    # An open project is its own place: progress, assistant and report.
    project_id = projects_ui.active_project_id()
    if project_id is not None:
        projects_ui.project_view(project_id, settings, render_report=render_report, restart=restart_research)
        return

    user = account.current_user()
    status = quota.status(user) if user else None
    ui.masthead(meta=[
        ("Model", _short_model(settings)),
        ("Status", "Ready" if credentials else "Key needed"),
        ("Credits", "Sign in" if status is None else "Unlimited" if status.unlimited
         else f"{status.remaining}/{status.limit}"),
    ])

    submitted, run_slot = search_panel(credentials=credentials, provider=settings.provider)
    submitted = submitted or bool(st.session_state.pop("autostart", False))

    with run_slot:
        if submitted:
            if not settings.query:
                ui.note("Enter a research question first.", status="warning")
            elif not credentials:
                env_var = PROVIDER_ENV.get(settings.provider, "ANTHROPIC_API_KEY")
                ui.note(
                    f"No API key found for {PROVIDERS[settings.provider]}. Add {env_var} to "
                    "the .env file in the project root, then restart the app.",
                    status="critical", label="Cannot run.",
                )
            else:
                run_with_quota(settings)

        if st.session_state.error:
            ui.note(st.session_state.error, status="critical", label="Run failed.")


@st.cache_resource(show_spinner=False)
def startup() -> list[str]:
    """Once per server process: migrate the database, create the admin, apply retention."""
    notices = []
    problem = auth.bootstrap_admin()
    if problem:
        notices.append(problem)
    community_service.apply_retention()
    projects.mark_interrupted()  # runs in flight when the app last stopped can never finish
    return notices


ASSETS = Path(__file__).resolve().parent / "assets"


def main() -> None:
    theme.inject()
    st.logo(str(ASSETS / "logo.svg"), size="large", icon_image=str(ASSETS / "icon.svg"))
    for notice in startup():
        st.warning(notice)
    account.apply_cookie_changes()
    message = st.session_state.pop("toast", None)
    if message:
        st.toast(message)

    user = account.current_user()
    nav.PAGES.update({
        "research": st.Page(research_page, title="Research", icon=":material/science:",
                            url_path="research", default=True),
        "insights": st.Page(insights_ui.insights_page, title="Insights", icon=":material/insights:",
                            url_path="insights"),
        "community": st.Page(community_ui.community_page, title="Community",
                             icon=":material/groups:", url_path="community"),
        "interactions": st.Page(community_ui.interactions_page, title="Food ↔ drug",
                                icon=":material/restaurant:", url_path="interactions"),
        "drugs": st.Page(community_ui.drug_interactions_page, title="Drug ↔ drug",
                         icon=":material/medication:", url_path="drugs"),
        "report": st.Page(community_ui.report_page, title="Report a side effect",
                          icon=":material/edit_note:", url_path="report"),
        "projects": st.Page(projects_ui.projects_page, title="Projects", icon=":material/folder_open:",
                            url_path="projects"),
        "plans": st.Page(account.plans_page, title="Plans & access",
                         icon=":material/workspace_premium:", url_path="plans"),
        "account": st.Page(account.account_page, title="Account" if user is None else user.username,
                           icon=":material/person:", url_path="account"),
        # Listed only for administrators; the page itself re-checks the role.
        "admin": st.Page(admin_ui.admin_page, title="Admin console", icon=":material/admin_panel_settings:",
                         url_path="admin"),
    })
    pages = {
        "Workspace": [nav.PAGES[k] for k in ("research", "projects", "insights", "community", "interactions",
                                             "drugs", "report")],
        "Account": [nav.PAGES["plans"], nav.PAGES["account"]]
                   + ([nav.PAGES["admin"]] if user and user.is_admin else []),
    }
    st.navigation(pages, position="sidebar", expanded=True).run()


if __name__ == "__main__":
    main()
