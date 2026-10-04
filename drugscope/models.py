"""Typed contracts for DrugScope.

Two families live here:

* **Retrieval types** (`SourceRecord`) - what the evidence layer returns. Plain
  dataclasses; they are built by code, never by a model.
* **Analysis types** (`ResearchPlan`, `RecordAppraisal`, the synthesis blocks) -
  what Claude returns under a JSON schema. These are Pydantic models with **no
  optional fields**, because strict JSON-schema output requires every property to
  be required; "unknown" is expressed as an empty string or -1.

Keeping the two families apart is what makes citations trustworthy: a model may
only ever *refer* to a `SourceRecord.sid` that the retrieval layer minted, and
the citation validator drops any handle it did not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #

RecordKind = Literal[
    "paper", "trial", "regulatory", "safety", "compound", "news", "document",
]

StudyDesign = Literal[
    "meta_analysis",
    "systematic_review",
    "rct",
    "cohort",
    "case_control",
    "cross_sectional",
    "case_report",
    "preclinical",
    "review",
    "guideline",
    "other",
]

# Deterministic evidence hierarchy. Higher is stronger.
DESIGN_RANK: dict[str, int] = {
    "meta_analysis": 7,
    "systematic_review": 7,
    "guideline": 6,
    "rct": 6,
    "cohort": 4,
    "case_control": 4,
    "cross_sectional": 3,
    "review": 2,
    "case_report": 2,
    "preclinical": 1,
    "other": 1,
}

DESIGN_LABELS: dict[str, str] = {
    "meta_analysis": "Meta-analysis",
    "systematic_review": "Systematic review",
    "guideline": "Clinical guideline",
    "rct": "Randomised trial",
    "cohort": "Cohort study",
    "case_control": "Case-control",
    "cross_sectional": "Cross-sectional",
    "review": "Narrative review",
    "case_report": "Case report",
    "preclinical": "Preclinical",
    "other": "Other",
}

TIER_ORDER = ["Tier 1", "Tier 2", "Tier 3", "Tier 4"]
TIER_MEANING = {
    "Tier 1": "Synthesised or randomised evidence in humans",
    "Tier 2": "Controlled observational evidence",
    "Tier 3": "Uncontrolled, descriptive or narrative evidence",
    "Tier 4": "Preclinical or mechanistic only",
}


@dataclass
class SourceRecord:
    """One retrieved artefact, addressable by a stable citation handle."""

    sid: str
    kind: RecordKind
    title: str
    url: str
    source: str                       # "PubMed", "ClinicalTrials.gov", "openFDA", ...
    snippet: str = ""                 # the text the analysis layer actually reads
    date: str = ""                    # ISO-8601 where the source gives one
    year: int | None = None
    venue: str = ""                   # journal, registry, agency
    authors: list[str] = field(default_factory=list)
    identifiers: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    design: StudyDesign = "other"     # heuristic at retrieval, refined by appraisal
    sample_size: int | None = None

    @property
    def tier(self) -> str:
        rank = DESIGN_RANK.get(self.design, 1)
        if rank >= 6:
            return "Tier 1"
        if rank >= 4:
            return "Tier 2"
        if rank >= 2:
            return "Tier 3"
        return "Tier 4"

    @property
    def citation(self) -> str:
        bits: list[str] = []
        if self.authors:
            bits.append(f"{self.authors[0]} et al." if len(self.authors) > 1 else self.authors[0])
        if self.venue:
            bits.append(self.venue)
        if self.year:
            bits.append(str(self.year))
        return ", ".join(bits) or self.source

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["tier"] = self.tier
        d["citation"] = self.citation
        return d


# --------------------------------------------------------------------------- #
# Stage 1 - planning
# --------------------------------------------------------------------------- #

class EntityRef(BaseModel):
    name: str = Field(description="Canonical name as it appears in the literature")
    kind: Literal["drug", "disease", "biologic", "target", "class", "procedure", "other"]
    aliases: list[str] = Field(description="Brand names, INN, synonyms, abbreviations")


class ResearchPlan(BaseModel):
    """What the question is actually asking, and how to go look it up."""

    interpretation: str = Field(description="One sentence restating the research question precisely")
    intent: Literal[
        "drug_profile", "disease_landscape", "treatment_comparison",
        "safety_review", "development_tracking", "mechanism", "general",
    ]
    primary_entities: list[EntityRef]
    comparators: list[EntityRef] = Field(
        description="Competing drugs or treatments worth benchmarking; empty list if none"
    )
    conditions: list[str] = Field(description="Diseases or indications in scope")
    literature_queries: list[str] = Field(
        description="2-5 PubMed boolean queries, most specific first, using MeSH-style terms and field tags"
    )
    trial_queries: list[str] = Field(
        description="1-3 short intervention or condition phrases for trial registries; no boolean operators"
    )
    regulatory_terms: list[str] = Field(
        description="1-4 generic or brand drug names for regulatory lookup; empty list for pure disease questions"
    )
    key_outcomes: list[str] = Field(
        description="Outcomes that matter for this question, e.g. 'HbA1c reduction', 'MACE', 'overall survival'"
    )
    open_questions: list[str] = Field(
        description="What a domain expert would want resolved before trusting a conclusion"
    )
    disclaimers: list[str] = Field(description="Scope limits worth stating up front; empty list if none")


# --------------------------------------------------------------------------- #
# Stage 2 - per-record appraisal
# --------------------------------------------------------------------------- #

class ExtractedClaim(BaseModel):
    statement: str = Field(description="A single, self-contained factual claim made by this record")
    topic: str = Field(
        description="Short normalised topic label, 2-5 words, reused verbatim across records where they discuss the same thing"
    )
    dimension: Literal["efficacy", "safety", "pharmacology", "mechanism", "economics", "access", "other"]
    direction: Literal["supports", "refutes", "neutral", "mixed"] = Field(
        description="Direction with respect to the intervention being effective or beneficial for this topic"
    )
    population: str = Field(description="Who was studied; empty string if unclear")
    effect: str = Field(description="Quantified effect with units and CI if reported; empty string if none")
    certainty: Literal["high", "moderate", "low"]


class RecordAppraisal(BaseModel):
    """Structured read of one retrieved record."""

    design: StudyDesign
    sample_size: int = Field(description="Participants analysed; -1 if not reported or not applicable")
    relevance: int = Field(description="0-100, how directly this record answers the research question")
    summary: str = Field(description="Two sentences: what was done, and what was found")
    claims: list[ExtractedClaim]
    limitations: list[str] = Field(description="Concrete methodological limitations; empty list if none evident")
    conflicts_of_interest: str = Field(description="Funding or COI signal if stated; empty string otherwise")


class HandledAppraisal(RecordAppraisal):
    """A `RecordAppraisal` that names the record it belongs to.

    Used only by backends that appraise several records per call - the handle is
    what maps each appraisal back to its source, since a batched reply has no
    positional guarantee.
    """

    handle: str = Field(description="The record handle this appraisal is for, exactly as given, e.g. 'S4'")


class AppraisalBatch(BaseModel):
    appraisals: list[HandledAppraisal] = Field(
        description="One entry per record supplied, in any order, each naming its handle"
    )


# --------------------------------------------------------------------------- #
# Stage 3 - synthesis
# --------------------------------------------------------------------------- #

class KeyFinding(BaseModel):
    headline: str = Field(description="The finding stated as a claim, not a topic label")
    detail: str = Field(description="2-4 sentences of substance, quantified wherever the evidence allows")
    so_what: str = Field(description="One sentence on why this matters for a decision-maker")
    strength: Literal["strong", "moderate", "limited", "preliminary"]
    citations: list[str] = Field(
        description="Source handles such as ['S3','T7'], drawn only from the provided corpus"
    )


class ComparisonCell(BaseModel):
    entity: str
    value: str = Field(description="Concise cell content; 'Not established' when the corpus is silent")
    citations: list[str]


class ComparisonRow(BaseModel):
    criterion: str = Field(description="What is being compared, e.g. 'Primary efficacy endpoint'")
    cells: list[ComparisonCell]
    verdict: str = Field(
        description="One line naming which option leads on this criterion and by how much; empty string if tied or unclear"
    )


class ComparisonMatrix(BaseModel):
    entities: list[str] = Field(description="The options being compared, 2-4 of them; empty list if no comparison applies")
    rows: list[ComparisonRow]
    bottom_line: str = Field(description="Where the evidence currently points, with the caveat that matters most")


class TimelineEvent(BaseModel):
    date: str = Field(description="YYYY, or YYYY-MM, or YYYY-MM-DD")
    label: str = Field(description="Short event title")
    category: Literal[
        "discovery", "preclinical", "trial", "regulatory",
        "safety", "publication", "commercial", "other",
    ]
    detail: str
    citations: list[str]


class EvidenceConflict(BaseModel):
    topic: str
    position_a: str = Field(description="One side of the disagreement, stated as a claim")
    citations_a: list[str]
    position_b: str = Field(description="The opposing claim")
    citations_b: list[str]
    assessment: str = Field(
        description="Which side the stronger evidence favours and why, or a statement that it is genuinely unresolved"
    )
    likely_explanation: str = Field(
        description="Plausible methodological reason for the divergence: population, dose, endpoint, follow-up, or bias"
    )
    severity: Literal["decision_changing", "material", "minor"]


class ResearchGap(BaseModel):
    question: str = Field(description="The unanswered question, phrased as a question")
    why_it_matters: str
    what_would_answer_it: str = Field(
        description="The study or dataset that would close the gap - design, population, endpoint, duration"
    )
    evidence_absent: str = Field(description="What the corpus conspicuously lacks on this point")
    priority: Literal["critical", "high", "moderate"]


class SafetySignal(BaseModel):
    event: str
    frequency: str = Field(description="Reported rate or count; empty string if unquantified")
    seriousness: Literal["critical", "serious", "moderate", "mild"]
    context: str = Field(description="Population, dose or duration in which it appears")
    citations: list[str]


class ExecutiveSummary(BaseModel):
    verdict: str = Field(description="The single most important takeaway, in one sentence")
    narrative: str = Field(
        description="3-5 paragraph briefing in plain prose, quantified, no bullet lists, no headings"
    )
    confidence: Literal["high", "moderate", "low"]
    confidence_rationale: str = Field(
        description="Why that confidence level, referencing the shape and limits of the evidence base"
    )
    consensus: int = Field(description="0-100: how much the retrieved evidence agrees with itself")
    maturity: int = Field(description="0-100: how far the subject has progressed from bench to established practice")
    watch_items: list[str] = Field(description="2-5 specific developments that would change the picture")


class SynthesisBundle(BaseModel):
    """Everything the synthesis pass returns in one structured payload."""

    summary: ExecutiveSummary
    key_findings: list[KeyFinding]
    comparison: ComparisonMatrix
    timeline: list[TimelineEvent]
    conflicts: list[EvidenceConflict]
    gaps: list[ResearchGap]
    safety_signals: list[SafetySignal]
    regulatory_status: str = Field(description="Prose paragraph on approvals, labels and outstanding submissions")
    mechanism: str = Field(
        description="Prose paragraph on mechanism of action or pathophysiology as the corpus describes it"
    )


# --------------------------------------------------------------------------- #
# Assembled report
# --------------------------------------------------------------------------- #

@dataclass
class Usage:
    """Token and cost accounting across every model call in one run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls
        self.cost_usd += other.cost_usd


@dataclass
class Report:
    query: str
    plan: ResearchPlan
    synthesis: SynthesisBundle
    records: list[SourceRecord]
    appraisals: dict[str, RecordAppraisal]
    metrics: dict[str, Any]
    usage: Usage
    elapsed_seconds: float
    depth: str
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    warnings: list[str] = field(default_factory=list)

    def record_by_sid(self, sid: str) -> SourceRecord | None:
        return next((r for r in self.records if r.sid == sid), None)

    def cited_records(self, sids: list[str]) -> list[SourceRecord]:
        out: list[SourceRecord] = []
        for sid in sids:
            rec = self.record_by_sid(sid)
            if rec is not None:
                out.append(rec)
        return out
