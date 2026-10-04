"""Deterministic analytics.

Every number on a KPI card and every chart series is computed here, in code, from
the retrieved corpus and its structured appraisals - never asked of a model. That
is a deliberate split of labour:

* A model is good at *reading* an abstract and saying what it claims.
* A model is bad at *counting*, and worse at counting consistently across runs.

So the model classifies, and arithmetic aggregates. The practical payoff is that
two runs over the same corpus produce the same Evidence Quality Index, and every
index can be opened up and explained component by component - which is what makes
the score defensible to a reviewer rather than a black box.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from ..models import (
    DESIGN_LABELS,
    DESIGN_RANK,
    TIER_ORDER,
    RecordAppraisal,
    SourceRecord,
)
from ..sources.trials import ACTIVE_STATUSES, PHASE_ORDER, PHASE_RANK, STOPPED_STATUSES

CURRENT_YEAR = datetime.now(timezone.utc).year


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


# --------------------------------------------------------------------------- #
# Composite indices
# --------------------------------------------------------------------------- #

def evidence_quality_index(
    records: list[SourceRecord],
    appraisals: dict[str, RecordAppraisal],
) -> dict[str, Any]:
    """A 0-100 read on how strong the retrieved evidence base is.

    Five weighted components, each independently interpretable:

    | Component      | Weight | What it measures                                  |
    |----------------|--------|---------------------------------------------------|
    | Best design    |   30   | The strongest study design present at all         |
    | Depth          |   25   | How much Tier 1 evidence there is, saturating at 5|
    | Human evidence |   15   | Share of records that are not preclinical         |
    | Recency        |   15   | Share published within five years                 |
    | Corroboration  |   15   | Corpus size, saturating at 20 records             |

    Deliberately *not* a quality score for any single study, and deliberately
    saturating: the 6th randomised trial adds less than the 2nd, which matches how
    evidence appraisal actually works.
    """
    scored = [r for r in records if r.kind in ("paper", "trial")]
    if not scored:
        return {"score": 0, "components": {}, "basis": 0}

    ranks = [DESIGN_RANK.get(r.design, 1) for r in scored]
    best_design = max(ranks) / 7.0
    tier1 = sum(1 for r in scored if r.tier == "Tier 1")
    depth = min(1.0, tier1 / 5.0)
    human = sum(1 for r in scored if r.design != "preclinical") / len(scored)
    recent = sum(1 for r in scored if r.year and r.year >= CURRENT_YEAR - 5) / len(scored)
    corroboration = min(1.0, len(scored) / 20.0)

    components = {
        "Best available design": (best_design, 30),
        "Depth of top-tier evidence": (depth, 25),
        "Human (non-preclinical) evidence": (human, 15),
        "Recency (within 5 years)": (recent, 15),
        "Corroboration (corpus size)": (corroboration, 15),
    }
    score = sum(value * weight for value, weight in components.values())

    return {
        "score": int(round(score)),
        "components": {
            name: {"normalised": round(value, 3), "weight": weight, "points": round(value * weight, 1)}
            for name, (value, weight) in components.items()
        },
        "basis": len(scored),
    }


def consensus_index(appraisals: dict[str, RecordAppraisal]) -> dict[str, Any]:
    """How much the corpus agrees with itself, per topic and overall.

    For each topic carrying at least two directional claims, agreement is the
    share held by the majority direction. Topics are then weighted by how many
    claims they carry, so a well-studied topic moves the index more than a topic
    mentioned twice. Neutral and mixed claims are excluded from the numerator but
    counted as evidence that the topic was examined.
    """
    by_topic: dict[str, Counter] = defaultdict(Counter)
    for appraisal in appraisals.values():
        for claim in appraisal.claims:
            key = claim.topic.strip().lower()
            if key:
                by_topic[key][claim.direction] += 1

    rows: list[dict[str, Any]] = []
    weighted_total = 0.0
    weight_sum = 0.0

    for topic, counts in by_topic.items():
        directional = counts["supports"] + counts["refutes"]
        total = sum(counts.values())
        if directional < 2:
            continue
        agreement = max(counts["supports"], counts["refutes"]) / directional
        rows.append({
            "topic": topic,
            "supports": counts["supports"],
            "refutes": counts["refutes"],
            "mixed": counts["mixed"] + counts["neutral"],
            "claims": total,
            "agreement": round(100 * agreement, 1),
            "contested": agreement < 0.7,
        })
        weighted_total += agreement * total
        weight_sum += total

    rows.sort(key=lambda r: (r["agreement"], -r["claims"]))
    return {
        "score": int(round(100 * weighted_total / weight_sum)) if weight_sum else 0,
        "topics_assessed": len(rows),
        "contested_topics": [r for r in rows if r["contested"]],
        "rows": rows,
    }


# --------------------------------------------------------------------------- #
# Stream-level breakdowns
# --------------------------------------------------------------------------- #

def literature_profile(records: list[SourceRecord]) -> dict[str, Any]:
    papers = [r for r in records if r.kind == "paper"]
    years = Counter(r.year for r in papers if r.year)
    designs = Counter(r.design for r in papers)
    tiers = Counter(r.tier for r in papers)
    journals = Counter(r.venue for r in papers if r.venue)

    span = sorted(years)
    by_year = [{"year": y, "papers": years[y]} for y in range(span[0], span[-1] + 1)] if span else []

    return {
        "count": len(papers),
        "by_year": by_year,
        "median_year": (
            sorted(r.year for r in papers if r.year)[
                sum(1 for r in papers if r.year) // 2
            ] if years else None
        ),
        "design_mix": [
            {"design": DESIGN_LABELS.get(d, d), "count": c}
            for d, c in sorted(designs.items(), key=lambda kv: -DESIGN_RANK.get(kv[0], 0))
        ],
        "tier_mix": [{"tier": t, "count": tiers.get(t, 0)} for t in TIER_ORDER],
        "top_journals": [{"journal": j, "count": c} for j, c in journals.most_common(8)],
        "total_citations": sum(int(r.meta.get("citations") or 0) for r in papers),
        "open_access": sum(1 for r in papers if r.meta.get("open_access")),
        "recent_share": _pct(
            sum(1 for r in papers if r.year and r.year >= CURRENT_YEAR - 3), len(papers)
        ),
    }


def trial_profile(records: list[SourceRecord]) -> dict[str, Any]:
    trials = [r for r in records if r.kind == "trial"]
    if not trials:
        return {"count": 0, "phase_mix": [], "status_mix": [], "sponsor_mix": []}

    phases = Counter(r.meta.get("phase", "Not applicable") for r in trials)
    statuses = Counter(r.meta.get("status", "Unknown status") for r in trials)
    sponsors = Counter(r.meta.get("sponsor_class", "Unknown") or "Unknown" for r in trials)
    enrollments = [int(r.meta["enrollment"]) for r in trials if r.meta.get("enrollment")]

    ranked_phases = sorted(
        (p for p in phases if p != "Not applicable"),
        key=lambda p: PHASE_RANK.get(p, -1),
    )
    largest = max(trials, key=lambda r: int(r.meta.get("enrollment") or 0))

    return {
        "count": len(trials),
        "registry_total": max((int(r.meta.get("registry_total") or 0) for r in trials), default=0),
        "phase_mix": [
            {"phase": p, "count": phases[p]}
            for p in PHASE_ORDER + [k for k in phases if k not in PHASE_ORDER]
            if phases.get(p)
        ],
        "status_mix": [{"status": s, "count": c} for s, c in statuses.most_common()],
        "sponsor_mix": [{"sponsor_class": s.title(), "count": c} for s, c in sponsors.most_common()],
        "active": sum(1 for r in trials if r.meta.get("status") in ACTIVE_STATUSES),
        "stopped": sum(1 for r in trials if r.meta.get("status") in STOPPED_STATUSES),
        "completed": sum(1 for r in trials if r.meta.get("status") == "Completed"),
        "with_results": sum(1 for r in trials if r.meta.get("has_results")),
        "highest_phase": ranked_phases[-1] if ranked_phases else "Not applicable",
        "total_enrollment": sum(enrollments),
        "median_enrollment": sorted(enrollments)[len(enrollments) // 2] if enrollments else 0,
        "largest_trial": {
            "title": largest.title,
            "nct": largest.identifiers.get("nct", ""),
            "enrollment": int(largest.meta.get("enrollment") or 0),
            "phase": largest.meta.get("phase", ""),
            "url": largest.url,
        },
        "industry_share": _pct(
            sum(1 for r in trials if "INDUSTRY" in str(r.meta.get("sponsor_class", "")).upper()),
            len(trials),
        ),
        "by_start_year": [
            {"year": y, "trials": c}
            for y, c in sorted(Counter(r.year for r in trials if r.year).items())
        ],
    }


def regulatory_profile(records: list[SourceRecord], safety: dict[str, Any]) -> dict[str, Any]:
    reg = [r for r in records if r.kind == "regulatory"]
    recalls = [r for r in records if r.kind == "safety"]

    approvals: list[dict[str, Any]] = []
    for record in reg:
        for submission in record.meta.get("submissions", []) or []:
            if submission.get("status") == "Approved" and submission.get("date"):
                approvals.append({
                    "date": submission["date"],
                    "application": record.identifiers.get("fda_application", ""),
                    "type": submission.get("type", ""),
                    "classification": submission.get("classification", ""),
                    "priority": submission.get("priority", ""),
                })
    approvals.sort(key=lambda a: a["date"])

    boxed = [r for r in reg if r.meta.get("has_boxed_warning")]

    return {
        "count": len(reg),
        "applications": sorted({r.identifiers.get("fda_application", "") for r in reg} - {""}),
        "approval_actions": len(approvals),
        "first_approval": approvals[0]["date"] if approvals else "",
        "latest_approval": approvals[-1]["date"] if approvals else "",
        "approvals": approvals,
        "priority_reviews": sum(1 for a in approvals if "PRIORITY" in a["priority"].upper()),
        "has_boxed_warning": bool(boxed),
        "boxed_warning_text": boxed[0].meta.get("boxed_warning", "") if boxed else "",
        "recalls": len(recalls),
        "critical_recalls": sum(1 for r in recalls if r.meta.get("severity") == "critical"),
        "faers": safety or {},
        "sponsors": sorted({r.meta.get("sponsor", "") for r in reg} - {""}),
    }


def claim_profile(appraisals: dict[str, RecordAppraisal]) -> dict[str, Any]:
    claims = [c for a in appraisals.values() for c in a.claims]
    if not claims:
        return {"count": 0, "dimension_mix": [], "direction_mix": [], "top_topics": []}

    dimensions = Counter(c.dimension for c in claims)
    directions = Counter(c.direction for c in claims)
    topics = Counter(c.topic.strip().lower() for c in claims if c.topic.strip())
    quantified = sum(1 for c in claims if c.effect.strip())

    return {
        "count": len(claims),
        "dimension_mix": [
            {"dimension": d.title(), "count": c} for d, c in dimensions.most_common()
        ],
        "direction_mix": [
            {"direction": d.title(), "count": directions.get(d, 0)}
            for d in ("supports", "mixed", "neutral", "refutes")
            if directions.get(d)
        ],
        "top_topics": [{"topic": t, "claims": c} for t, c in topics.most_common(12)],
        "quantified_share": _pct(quantified, len(claims)),
        "high_certainty_share": _pct(sum(1 for c in claims if c.certainty == "high"), len(claims)),
    }


def coverage_matrix(appraisals: dict[str, RecordAppraisal]) -> dict[str, Any]:
    """Where the evidence is thin - the empirical half of gap analysis.

    Cross-tabulates claim dimension against the strongest design that supports
    it. An empty or near-empty cell is a gap the model does not have to be
    trusted to notice, because the arithmetic already found it.
    """
    grid: dict[str, Counter] = defaultdict(Counter)
    dimensions = ["efficacy", "safety", "pharmacology", "mechanism", "economics", "access"]

    for appraisal in appraisals.values():
        tier = _design_tier(appraisal.design)
        for claim in appraisal.claims:
            if claim.dimension in dimensions:
                grid[claim.dimension][tier] += 1

    cells = [
        {
            "dimension": dim.title(),
            "tier": tier,
            "count": grid[dim].get(tier, 0),
        }
        for dim in dimensions
        for tier in TIER_ORDER
    ]
    thin = [
        {"dimension": dim.title(), "claims": sum(grid[dim].values())}
        for dim in dimensions
        if sum(grid[dim].values()) <= 1
    ]

    return {"cells": cells, "thin_dimensions": thin}


def _design_tier(design: str) -> str:
    rank = DESIGN_RANK.get(design, 1)
    if rank >= 6:
        return "Tier 1"
    if rank >= 4:
        return "Tier 2"
    if rank >= 2:
        return "Tier 3"
    return "Tier 4"


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def compute(
    records: list[SourceRecord],
    appraisals: dict[str, RecordAppraisal],
    safety: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the dashboard needs, in one dict."""
    literature = literature_profile(records)
    trials = trial_profile(records)
    regulatory = regulatory_profile(records, safety or {})
    claims = claim_profile(appraisals)
    quality = evidence_quality_index(records, appraisals)
    consensus = consensus_index(appraisals)

    relevances = [a.relevance for a in appraisals.values() if a.relevance >= 0]

    return {
        "totals": {
            "records": len(records),
            "papers": literature["count"],
            "trials": trials["count"],
            "regulatory": regulatory["count"],
            "appraised": len(appraisals),
            "claims": claims["count"],
            "sources_used": sorted({r.source for r in records}),
        },
        "literature": literature,
        "trials": trials,
        "regulatory": regulatory,
        "claims": claims,
        "quality": quality,
        "consensus": consensus,
        "coverage": coverage_matrix(appraisals),
        "mean_relevance": int(round(sum(relevances) / len(relevances))) if relevances else 0,
        "year_span": (
            f"{min(r.year for r in records if r.year)}-{max(r.year for r in records if r.year)}"
            if any(r.year for r in records) else "n/a"
        ),
    }
