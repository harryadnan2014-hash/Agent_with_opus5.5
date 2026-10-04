"""The LangGraph research pipeline, with a fake model and no network."""

import asyncio
import time

import pytest

from drugscope import pipeline
from drugscope.config import ResearchDepth, RunSettings, SourceToggles
from drugscope.llm import Capabilities
from drugscope.models import (
    AppraisalBatch, ComparisonMatrix, EntityRef, ExecutiveSummary, ExtractedClaim, HandledAppraisal,
    KeyFinding, ResearchPlan, SourceRecord, SynthesisBundle, Usage,
)


class FakeBrain:
    provider = "openrouter"
    model = "fake/model:free"
    appraisal_batch_size = 3
    is_free_tier = True

    def __init__(self, *, web: bool, fail_plan: bool = False):
        self.usage, self.caps = Usage(), Capabilities()
        self.supports_web_search = web
        self.fail_plan = fail_plan
        self.appraise_started = self.sweep_started = None

    async def structured(self, *, schema_model, user, **kw):
        if schema_model is ResearchPlan:
            if self.fail_plan:
                raise RuntimeError("simulated provider failure")
            return ResearchPlan(
                interpretation="q", intent="drug_profile",
                primary_entities=[EntityRef(name="semaglutide", kind="drug", aliases=[])],
                comparators=[], conditions=["obesity"], literature_queries=["q"], trial_queries=["q"],
                regulatory_terms=["semaglutide"], key_outcomes=["mace"], open_questions=[], disclaimers=[],
            )
        if schema_model is AppraisalBatch:
            self.appraise_started = self.appraise_started or time.monotonic()
            await asyncio.sleep(0.2)
            handles = [l.split("[")[1].split("]")[0] for l in user.splitlines() if l.startswith("HANDLE: [")]
            return AppraisalBatch(appraisals=[HandledAppraisal(
                handle=h, design="rct", sample_size=10, relevance=80, summary="s", limitations=[],
                conflicts_of_interest="", claims=[ExtractedClaim(
                    statement="c", topic="mace", dimension="efficacy", direction="supports",
                    population="", effect="", certainty="high")],
            ) for h in handles])
        if schema_model is SynthesisBundle:
            return SynthesisBundle(
                summary=ExecutiveSummary(verdict="v [S1] [ZZ9]", narrative="n [S1]", confidence="high",
                                         confidence_rationale="r", consensus=1, maturity=1, watch_items=[]),
                key_findings=[KeyFinding(headline="h", detail="d", so_what="s", strength="strong",
                                         citations=["S1", "FAKE7"])],
                comparison=ComparisonMatrix(entities=[], rows=[], bottom_line=""),
                timeline=[], conflicts=[], gaps=[], safety_signals=[], regulatory_status="", mechanism="",
            )
        raise AssertionError(schema_model)

    async def close(self):
        pass


@pytest.fixture
def offline(monkeypatch):
    async def fake_retrieve(fetcher, settings, plan, emit):
        emit("Querying 0 endpoints", 18)
        corpus = pipeline._Corpus()
        corpus.records = [SourceRecord(sid="", kind="paper", title=f"Paper {i}", url=f"https://x/{i}",
                                       source="PubMed", snippet="abstract", year=2024, design="rct")
                          for i in range(7)]
        corpus.warnings = ["retrieval note"]
        return corpus

    async def fake_sweep(brain, plan, *, model, query, **kw):
        brain.sweep_started = time.monotonic()
        await asyncio.sleep(0.2)
        return "Briefing.", [SourceRecord(sid="", kind="news", title="News", url="https://fda.gov/n", source="Web")]

    monkeypatch.setattr(pipeline, "_retrieve", fake_retrieve)
    monkeypatch.setattr(pipeline.websweep, "recent_developments", fake_sweep)


def _run(monkeypatch, brain):
    monkeypatch.setattr(pipeline, "make_brain", lambda **kw: brain)
    settings = RunSettings(query="q", depth=ResearchDepth.STANDARD, toggles=SourceToggles(),
                           provider="openrouter", gateway_model=brain.model)
    return list(pipeline.run_sync(settings))


def test_graph_with_parallel_sweep(offline, monkeypatch):
    brain = FakeBrain(web=True)
    events = _run(monkeypatch, brain)
    stages = list(dict.fromkeys(e.stage for e in events))
    assert stages == ["plan", "retrieve", "appraise", "sweep", "measure", "synthesise", "audit", "done"]
    assert [e.pct for e in events] == sorted(e.pct for e in events)
    report = events[-1].payload
    assert sum(r.kind == "news" for r in report.records) == 1 and report.records[-1].sid == "W1"
    assert report.metrics["recent_developments"] == "Briefing."
    assert abs(brain.sweep_started - brain.appraise_started) < 0.15  # branches ran together
    # The citation audit still strips anything retrieval did not mint.
    assert report.synthesis.key_findings[0].citations == ["S1"]
    assert "[ZZ9]" not in report.synthesis.summary.verdict
    assert "retrieval note" in report.warnings


def test_graph_without_sweep_branch(offline, monkeypatch):
    events = _run(monkeypatch, FakeBrain(web=False))
    assert "sweep" not in {e.stage for e in events}
    report = events[-1].payload
    assert any("sweep skipped" in w for w in report.warnings)
    assert [e.label for e in events if e.label.startswith("Appraising")][-1] == "Appraising evidence (7/7)"


def test_graph_failure_becomes_error_event(offline, monkeypatch):
    events = _run(monkeypatch, FakeBrain(web=False, fail_plan=True))
    assert events[-1].stage == "error" and "simulated provider failure" in events[-1].detail


def test_graph_is_a_langgraph_state_graph():
    from langgraph.graph.state import CompiledStateGraph

    graph = pipeline.build_graph(RunSettings(query="q"), FakeBrain(web=True), None, 0.0)
    assert isinstance(graph, CompiledStateGraph)
    nodes = set(graph.get_graph().nodes)
    assert {"plan", "retrieve", "appraise", "sweep", "measure", "synthesise", "audit"} <= nodes
