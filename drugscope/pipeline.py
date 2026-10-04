"""The research pipeline, as a LangGraph state graph.

A **code-controlled workflow**, not an open-ended agent - and that is the central
design decision in this project, so it is worth stating why.

An agent given search tools would decide for itself what to look up. That sounds
better and is worse here, for three reasons: retrieval from nine well-understood
research APIs is a solved problem that does not need a model's judgement; a fixed
sequence of stages can be streamed to a progress bar with honest percentages; and
- most importantly - every citation in the finished report resolves to a record
that a deterministic HTTP call actually returned. The model never supplies a
reference. It can only ever *cite* one that retrieval minted, and
`audit_citations` verifies that claim afterwards.

LangGraph runs the stages, but as a graph whose **edges are fixed in code**. The
only branch is decided by configuration (is the web sweep on, and can this provider
do it), never by a model, and no node hands the model a retrieval tool - so the
citation guarantee is exactly as strong as it would be in hand-written control
flow. What the graph adds is structure: typed state passed between nodes, the
appraisal and web-sweep branches running in parallel and joining before
measurement, and progress streamed out of every node as it works.

    START -> plan -> retrieve -+-> appraise --+-> measure -> synthesise -> audit -> END
                               +-> sweep -----+   (sweep only when enabled)

1. **Plan** - one model call turns the question into boolean queries and entities.
2. **Retrieve** - every query against every enabled stream, concurrently.
3. **Appraise || sweep** - one model call per record, with the web sweep alongside.
4. **Measure** - deterministic analytics over the appraised corpus.
5. **Synthesise** - one model call over the structured digest.
6. **Audit** - strip any citation that does not resolve, then assemble.

`run()` is an async generator of `Progress` events ending in a `Report`, so a front
end can render the run as it happens without knowing anything about the stages.
"""

from __future__ import annotations

import asyncio
import logging
import operator
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, AsyncIterator, Callable, Literal, TypedDict

from .analysis import extract, metrics as metrics_mod, plan as plan_mod, synthesize, websweep
from .config import (
    PLANNING_MODEL,
    RunSettings,
    SYNTHESIS_MODEL,
)
from .llm import LLMError
from .providers import make_brain
from .models import (
    DESIGN_RANK,
    RecordAppraisal,
    Report,
    ResearchPlan,
    SourceRecord,
    SynthesisBundle,
)
from .sources import documents as documents_src, literature, pharmacology, regulatory, trials
from .sources.base import Fetcher, dedupe

log = logging.getLogger("drugscope.pipeline")


def get_stream_writer():
    """LangGraph's per-node progress writer. LangGraph is imported on the first run,
    not at app start - it costs ~2-3 s to import and the home page never needs it."""
    from langgraph.config import get_stream_writer as writer

    return writer()

# Citation handle prefixes, by record kind. These appear in the finished report, so
# they are chosen to be readable: S for study, T for trial, R for regulatory.
_PREFIX = {
    "paper": "S",
    "trial": "T",
    "regulatory": "R",
    "safety": "F",
    "compound": "C",
    "news": "W",
    "document": "D",
}

StageName = Literal[
    "plan", "retrieve", "appraise", "sweep", "measure", "synthesise", "audit", "done", "error"
]


@dataclass
class Progress:
    """One observable moment in a run."""

    stage: StageName
    label: str
    detail: str = ""
    pct: int = 0
    thinking: str = ""
    payload: Any = None


@dataclass
class _Corpus:
    records: list[SourceRecord] = field(default_factory=list)
    safety: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def mint_sids(self) -> None:
        """Assign stable citation handles, grouped by kind."""
        counters: dict[str, int] = {}
        order = {"paper": 0, "trial": 1, "regulatory": 2, "safety": 3,
                 "compound": 4, "document": 5, "news": 6}
        self.records.sort(key=lambda r: order.get(r.kind, 9))
        for record in self.records:
            prefix = _PREFIX.get(record.kind, "X")
            counters[prefix] = counters.get(prefix, 0) + 1
            record.sid = f"{prefix}{counters[prefix]}"


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

def _rank_papers(records: list[SourceRecord]) -> list[SourceRecord]:
    """Deterministic literature ranking, applied before the cap.

    Strongest design first, then citation weight, then recency. Citations are
    log-ish-bucketed rather than used raw so that one 5,000-citation review cannot
    outrank an entire tier of randomised trials.
    """
    def key(record: SourceRecord) -> tuple[int, int, int]:
        citations = int(record.meta.get("citations") or 0)
        bucket = 0 if citations < 10 else 1 if citations < 100 else 2 if citations < 1000 else 3
        return (DESIGN_RANK.get(record.design, 1), bucket, record.year or 0)

    return sorted(records, key=key, reverse=True)


def _rank_others(records: list[SourceRecord], limit: int) -> list[SourceRecord]:
    """Keep the regulatory records that carry information, up to the depth's limit.

    Drugs@FDA returns one application per generic manufacturer, so a well-known drug
    brings back a dozen near-identical ANDA records that each cost an appraisal call
    and add nothing. Labels, original NDA/BLA approvals, curated pharmacology and
    recalls are kept first; generic applications only fill what room is left.
    """
    def priority(record: SourceRecord) -> int:
        if record.kind == "compound":
            return 0
        if record.source == "FDA label":
            return 1
        app = record.identifiers.get("fda_application", "")
        if app.startswith(("NDA", "BLA")):
            return 2
        if record.kind == "safety":
            return 3 if record.meta.get("severity") == "critical" else 5
        return 4  # ANDA and anything else

    ranked = sorted(records, key=priority)
    compounds = [r for r in ranked if r.kind == "compound"]
    rest = [r for r in ranked if r.kind != "compound"]
    return compounds + rest[:limit]


async def _normalize_drug_names(fetcher: Fetcher, names: list[str], corpus: _Corpus) -> list[str]:
    """Map the plan's drug names to RxNorm active ingredients, keeping order.

    The planner usually gets names right, but not always: a brand name, a salt form
    or a typo in the question can reach the regulatory queries, which match on the
    generic name and would come back empty. Unrecognised names are kept as written.
    """
    resolved = await asyncio.gather(
        *(pharmacology.normalize_drug(fetcher, name) for name in names[:6]), return_exceptions=True
    )
    out: list[str] = []
    for name, match in zip(names, resolved):
        ingredient = match["ingredient"] if isinstance(match, dict) else name.lower()
        if isinstance(match, dict) and ingredient != name.lower():
            corpus.warnings.append(f"'{name}' resolved to the active ingredient '{ingredient}' (RxNorm)")
        if ingredient not in out:
            out.append(ingredient)
    return out + [n for n in names[6:] if n.lower() not in out]


async def _retrieve(
    fetcher: Fetcher,
    settings: RunSettings,
    research_plan: ResearchPlan,
    emit: Callable[[str, int], None],
) -> _Corpus:
    corpus = _Corpus()
    profile = settings.profile
    toggles = settings.toggles

    drug_names = [
        e.name for e in (research_plan.primary_entities + research_plan.comparators)
        if e.kind in ("drug", "biologic")
    ] or list(research_plan.regulatory_terms)
    drug_names = await _normalize_drug_names(fetcher, drug_names, corpus)

    tasks: list[Any] = []
    kinds: list[str] = []

    if toggles.literature:
        per_query = max(8, profile.papers // max(1, len(research_plan.literature_queries)))
        for query in research_plan.literature_queries[:5]:
            tasks.append(literature.search_europepmc(
                fetcher, query, limit=per_query, year_from=settings.year_from
            ))
            kinds.append("literature")
            tasks.append(literature.search_pubmed(
                fetcher, query, limit=per_query, year_from=settings.year_from
            ))
            kinds.append("literature")

    if toggles.trials:
        per_query = max(10, profile.trials // max(1, len(research_plan.trial_queries) or 1))
        for phrase in (research_plan.trial_queries or [settings.query])[:3]:
            tasks.append(trials.search_trials(fetcher, intervention=phrase, limit=per_query))
            kinds.append("trials")
        for condition in research_plan.conditions[:2]:
            tasks.append(trials.search_trials(fetcher, condition=condition, limit=per_query))
            kinds.append("trials")

    if toggles.regulatory:
        for name in drug_names[:4]:
            tasks.append(regulatory.search_approvals(fetcher, name, limit=profile.regulatory))
            kinds.append("regulatory")
            tasks.append(regulatory.search_labels(fetcher, name, limit=2))
            kinds.append("regulatory")
            tasks.append(regulatory.search_recalls(fetcher, name, limit=4))
            kinds.append("regulatory")

    if toggles.pharmacology:
        for name in drug_names[:4]:
            tasks.append(_pharmacology_record(fetcher, name))
            kinds.append("pharmacology")

    emit(f"Querying {len(tasks)} endpoints across enabled sources", 18)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    papers: list[SourceRecord] = []
    trial_records: list[SourceRecord] = []
    others: list[SourceRecord] = []

    for kind, result in zip(kinds, results):
        if isinstance(result, BaseException):
            corpus.warnings.append(f"{kind} query failed: {type(result).__name__}")
            continue
        batch = result if isinstance(result, list) else ([result] if result else [])
        for record in batch:
            if record is None:
                continue
            if record.kind == "paper":
                papers.append(record)
            elif record.kind == "trial":
                trial_records.append(record)
            else:
                others.append(record)

    emit("De-duplicating and ranking retrieved evidence", 30)

    papers = _rank_papers(dedupe(papers))[: profile.papers]
    trial_records = trials.prioritise(dedupe(trial_records), profile.trials)
    others = _rank_others(dedupe(others), profile.regulatory)

    # FAERS is an aggregate query rather than a record, so it rides alongside.
    if toggles.regulatory and drug_names:
        corpus.safety = await regulatory.adverse_event_profile(fetcher, drug_names[0], top=20)

    # User uploads join the same corpus: chunked, ranked against the planned
    # query terms, and appraised like any retrieved record.
    uploaded: list[SourceRecord] = []
    if settings.documents:
        emit("Indexing uploaded documents", 34)
        terms = (
            research_plan.key_outcomes
            + research_plan.conditions
            + [e.name for e in research_plan.primary_entities]
            + [settings.query]
        )
        uploaded, doc_warnings = documents_src.build_records(
            settings.documents, terms, limit=max(8, profile.papers // 3)
        )
        corpus.warnings.extend(doc_warnings)

    corpus.records = papers + trial_records + others + uploaded
    corpus.warnings.extend(fetcher.warnings)
    return corpus


async def _pharmacology_record(fetcher: Fetcher, name: str) -> SourceRecord | None:
    rxnorm, chembl = await asyncio.gather(
        pharmacology.resolve_rxnorm(fetcher, name),
        pharmacology.chembl_profile(fetcher, name),
    )
    return pharmacology.to_record(name, rxnorm, chembl)


# --------------------------------------------------------------------------- #
# The graph
# --------------------------------------------------------------------------- #

class RunState(TypedDict, total=False):
    """What flows between the nodes. Each node returns only the keys it sets."""

    plan: ResearchPlan
    corpus: _Corpus
    appraisals: dict[str, RecordAppraisal]
    briefing: str
    news: list[SourceRecord]
    metrics: dict[str, Any]
    bundle: SynthesisBundle
    report: Report
    # Appended to by parallel branches, so it needs a reducer rather than the
    # default last-write-wins channel.
    warnings: Annotated[list[str], operator.add]


def _thinking_relay(stage: StageName, label: str, pct: int) -> tuple[Callable[[str], None], list[str]]:
    """Stream the model's reasoning summary to the UI in chunks as it arrives."""
    write = get_stream_writer()
    chunks: list[str] = []
    sent = {"at": 0}

    def relay(chunk: str) -> None:
        chunks.append(chunk)
        size = sum(len(c) for c in chunks)
        if size - sent["at"] >= 400:
            sent["at"] = size
            write(Progress(stage=stage, label=label, pct=pct, thinking="".join(chunks)[-1500:]))

    return relay, chunks


def build_graph(settings: RunSettings, brain: Any, fetcher: Fetcher, started: float):
    """Compile the research graph for one run.

    Nodes close over the run's backend and HTTP session rather than carrying them
    in state: they are resources, not data, and keeping them out of state is what
    lets the state stay plain and inspectable.
    """
    from langgraph.graph import END, START, StateGraph

    profile = settings.profile
    # On a single-model gateway every stage runs the same model; on Anthropic each
    # stage picks its own. Asking the backend keeps the stages provider-agnostic.
    on_gateway = getattr(brain, "provider", "anthropic") != "anthropic"
    planning_model = brain.model if on_gateway else PLANNING_MODEL
    reasoning_model = brain.model if on_gateway else SYNTHESIS_MODEL
    worker_model = brain.model if on_gateway else settings.extraction_model
    wants_sweep = profile.web_search and settings.toggles.recent_news

    # -- 1. plan ---------------------------------------------------------------
    async def plan_node(state: RunState) -> dict[str, Any]:
        write = get_stream_writer()
        relay, thinking = _thinking_relay("plan", "Interpreting the research question", 8)
        research_plan = await plan_mod.build_plan(
            brain,
            settings.query,
            model=planning_model,
            comparators=settings.comparators,
            focus=settings.focus,
            on_thinking=relay,
        )
        write(Progress(
            stage="plan",
            label="Retrieval plan ready",
            detail=research_plan.interpretation,
            pct=12,
            thinking="".join(thinking)[-1200:],
            payload=research_plan,
        ))
        return {"plan": research_plan}

    # -- 2. retrieve -------------------------------------------------------------
    async def retrieve_node(state: RunState) -> dict[str, Any]:
        write = get_stream_writer()
        corpus = await _retrieve(
            fetcher,
            settings,
            state["plan"],
            lambda label, pct: write(Progress(stage="retrieve", label=label, pct=pct)),
        )
        corpus.mint_sids()

        if not corpus.records:
            raise LLMError(
                "No evidence was retrieved for this question. Try broader wording, "
                "a generic drug name instead of a brand name, or fewer filters."
            )

        counts = {
            kind: sum(1 for r in corpus.records if r.kind == kind)
            for kind in ("paper", "trial", "regulatory", "safety", "compound", "document")
        }
        write(Progress(
            stage="retrieve",
            label=f"Retrieved {len(corpus.records)} records",
            detail=(
                f"{counts['paper']} papers, {counts['trial']} trials, "
                f"{counts['regulatory']} regulatory, {counts['compound']} pharmacology"
                + (f", {counts['document']} uploaded" if counts["document"] else "")
            ),
            pct=38,
            payload=corpus.records,
        ))
        warnings = list(corpus.warnings)
        corpus.warnings = []  # handed to the reducer; not kept twice
        if wants_sweep and not brain.supports_web_search:
            warnings.append(
                "Recent-developments sweep skipped: server-side web search is an "
                "Anthropic feature and is not available on this provider."
            )
        return {"corpus": corpus, "warnings": warnings}

    def after_retrieve(state: RunState) -> list[str]:
        """The one branch in the graph - decided by configuration, not by a model."""
        if wants_sweep and brain.supports_web_search:
            return ["appraise", "sweep"]
        return ["appraise"]

    # -- 3a. appraise --------------------------------------------------------------
    async def appraise_node(state: RunState) -> dict[str, Any]:
        write = get_stream_writer()

        def on_record(done: int, total: int) -> None:
            write(Progress(
                stage="appraise",
                label=f"Appraising evidence ({done}/{total})",
                pct=38 + int(38 * done / max(total, 1)),
            ))

        appraisals, appraise_warnings = await extract.appraise_all(
            brain,
            state["corpus"].records,
            state["plan"],
            model=worker_model,
            effort=profile.extraction_effort,
            on_progress=on_record,
            batch_size=brain.appraisal_batch_size,
        )
        return {"appraisals": appraisals, "warnings": appraise_warnings}

    # -- 3b. web sweep (parallel with appraisal) -----------------------------------
    async def sweep_node(state: RunState) -> dict[str, Any]:
        briefing, news_records = await websweep.recent_developments(
            brain,
            state["plan"],
            model=reasoning_model,
            query=settings.query,
        )
        return {"briefing": briefing, "news": news_records}

    # -- 4. measure ----------------------------------------------------------------
    async def measure_node(state: RunState) -> dict[str, Any]:
        write = get_stream_writer()
        corpus = state["corpus"]
        appraisals = state.get("appraisals") or {}

        news = state.get("news") or []
        if news:
            for i, record in enumerate(news, 1):
                record.sid = f"W{i}"
            corpus.records.extend(news)
            write(Progress(
                stage="sweep",
                label=f"Web sweep added {len(news)} recent sources",
                pct=78,
            ))

        write(Progress(
            stage="appraise",
            label=f"Appraised {len(appraisals)} of {len(corpus.records)} records",
            detail=f"{sum(len(a.claims) for a in appraisals.values())} structured claims extracted",
            pct=80,
            payload=appraisals,
        ))

        write(Progress(stage="measure", label="Computing analytics and evidence indices", pct=82))
        computed = metrics_mod.compute(corpus.records, appraisals, corpus.safety)
        computed["recent_developments"] = state.get("briefing", "")
        write(Progress(
            stage="measure",
            label="Analytics ready",
            detail=(
                f"Evidence Quality {computed['quality']['score']}/100 - "
                f"Consensus {computed['consensus']['score']}/100"
            ),
            pct=85,
            payload=computed,
        ))
        return {"corpus": corpus, "metrics": computed}

    # -- 5. synthesise ---------------------------------------------------------------
    async def synthesise_node(state: RunState) -> dict[str, Any]:
        write = get_stream_writer()
        computed = state["metrics"]
        digest = synthesize.build_digest(
            state["corpus"].records, state.get("appraisals") or {}, computed
        )
        briefing = computed.get("recent_developments", "")
        if briefing and not briefing.startswith("("):
            digest += (
                "\n## Recent-developments briefing (open web, weakest evidence tier)\n\n"
                + briefing
                + "\n"
            )

        label = "Synthesising the report"
        write(Progress(
            stage="synthesise",
            label=label,
            detail="Reasoning over the full corpus - this is the longest step",
            pct=87,
        ))
        relay, thinking = _thinking_relay("synthesise", label, 90)
        bundle: SynthesisBundle = await synthesize.synthesize(
            brain,
            state["plan"],
            digest,
            query=settings.query,
            model=reasoning_model,
            effort=profile.effort,
            on_thinking=relay,
        )
        write(Progress(
            stage="synthesise",
            label="Synthesis complete",
            pct=96,
            thinking="".join(thinking)[-2000:],
        ))
        return {"bundle": bundle}

    # -- 6. audit ------------------------------------------------------------------
    async def audit_node(state: RunState) -> dict[str, Any]:
        write = get_stream_writer()
        write(Progress(stage="audit", label="Auditing citations against the corpus", pct=97))
        corpus = state["corpus"]
        bundle = state["bundle"]
        appraisals = state.get("appraisals") or {}
        citation_problems = synthesize.audit_citations(bundle, {r.sid for r in corpus.records})
        # After citations are cleaned: check each finding's figures against what it cites.
        numbers = synthesize.check_numbers(bundle, {r.sid: r for r in corpus.records}, appraisals)
        metrics = dict(state["metrics"], number_check=numbers)
        if numbers["unverified"]:
            citation_problems.append(
                f"{numbers['unverified']} of {numbers['checked']} figures in key findings were not found "
                "verbatim in the sources those findings cite; each is flagged on its finding"
            )

        report = Report(
            query=settings.query,
            plan=state["plan"],
            synthesis=bundle,
            records=corpus.records,
            appraisals=appraisals,
            metrics=metrics,
            usage=brain.usage,
            elapsed_seconds=round(time.monotonic() - started, 1),
            depth=settings.depth.value,
            warnings=list(state.get("warnings") or []) + citation_problems + list(brain.caps.notes),
        )
        write(Progress(
            stage="done",
            label="Report ready",
            detail=(
                f"{len(corpus.records)} sources - {report.elapsed_seconds}s - "
                f"${brain.usage.cost_usd:.2f}"
            ),
            pct=100,
            payload=report,
        ))
        return {"report": report}

    graph = StateGraph(RunState)
    graph.add_node("plan", plan_node)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("appraise", appraise_node)
    graph.add_node("sweep", sweep_node)
    graph.add_node("measure", measure_node)
    graph.add_node("synthesise", synthesise_node)
    graph.add_node("audit", audit_node)

    graph.add_edge(START, "plan")
    graph.add_edge("plan", "retrieve")
    graph.add_conditional_edges("retrieve", after_retrieve, ["appraise", "sweep"])
    # Both branches feed measure; when they run together, measure waits for both.
    graph.add_edge("appraise", "measure")
    graph.add_edge("sweep", "measure")
    graph.add_edge("measure", "synthesise")
    graph.add_edge("synthesise", "audit")
    graph.add_edge("audit", END)
    return graph.compile()


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #

async def run(settings: RunSettings) -> AsyncIterator[Progress]:
    """Execute one research run, yielding progress until the final report."""
    started = time.monotonic()
    yield Progress(stage="plan", label="Interpreting the research question", pct=4)

    brain: Any = None
    try:
        brain = make_brain(
            provider=settings.provider,
            model=settings.gateway_model,
            concurrency=settings.profile.concurrency,
        )
        async with Fetcher() as fetcher:
            graph = build_graph(settings, brain, fetcher, started)
            # "custom" mode carries the Progress events nodes write as they work.
            async for event in graph.astream({"warnings": []}, stream_mode="custom"):
                if isinstance(event, Progress):
                    yield event

    except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
        log.exception("run failed")
        yield Progress(
            stage="error",
            label=type(exc).__name__,
            detail=str(exc),
            pct=100,
            payload=exc,
        )
    finally:
        if brain is not None:
            await brain.close()


# --------------------------------------------------------------------------- #
# Sync bridge for Streamlit
# --------------------------------------------------------------------------- #

def run_sync(settings: RunSettings):
    """Drive the async generator from synchronous code.

    Streamlit's script model is synchronous, so the event loop is owned here and
    pumped one event at a time. This keeps the pipeline itself free of any
    front-end assumptions - the same generator works from a CLI or a web handler.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        agen = run(settings)
        while True:
            try:
                yield loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()
