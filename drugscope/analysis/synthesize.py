"""Stage 3 - synthesis, and the citation audit that follows it.

The synthesis call is the only one that sees the whole corpus at once. It gets the
per-record appraisals from stage 2 rather than the raw abstracts, which means it is
reasoning over structured claims instead of re-reading 60 abstracts - cheaper,
and far more consistent in what it notices.

It is also handed the **deterministic analytics** from `metrics.py`: the contested
topics the consensus index already found, and the thin cells the coverage matrix
already found. The model is not asked to discover conflicts and gaps from scratch;
it is asked to explain and adjudicate the ones arithmetic has already located, and
to add any it can see that counting cannot. That division is what keeps this stage
honest - the model cannot quietly fail to notice an inconvenient disagreement,
because the disagreement is printed in its prompt.

`audit_citations` then verifies every handle the model emitted against the corpus,
strips the ones that do not exist, and reports what it stripped.
"""

from __future__ import annotations

import re
from typing import Any

from ..config import MAX_TOKENS_SYNTHESIS, SYNTHESIS_MODEL
from ..llm import Brain, ThinkingSink
from ..models import (
    DESIGN_LABELS,
    RecordAppraisal,
    ResearchPlan,
    SourceRecord,
    SynthesisBundle,
)
from .prompts import SYSTEM, corpus_block

# --------------------------------------------------------------------------- #
# Corpus digest
# --------------------------------------------------------------------------- #

def _render_appraised(record: SourceRecord, appraisal: RecordAppraisal | None) -> str:
    header_bits = [
        f"[{record.sid}]",
        record.title,
        "|",
        record.venue or record.source,
    ]
    if record.year:
        header_bits.append(str(record.year))
    header_bits += ["|", DESIGN_LABELS.get(record.design, record.design), f"({record.tier})"]
    if record.sample_size:
        header_bits.append(f"| n={record.sample_size:,}")
    if record.meta.get("citations"):
        header_bits.append(f"| cited {record.meta['citations']}x")

    lines = [" ".join(header_bits)]

    if appraisal is None:
        # No structured read, so give the model the raw text rather than nothing.
        lines.append(f"  (not appraised) {record.snippet[:600]}")
        return "\n".join(lines)

    lines.append(f"  relevance {appraisal.relevance}/100 | {appraisal.summary}")
    for claim in appraisal.claims:
        effect = f' effect: "{claim.effect}"' if claim.effect.strip() else ""
        population = f" in {claim.population}" if claim.population.strip() else ""
        lines.append(
            f"  - [{claim.dimension}/{claim.direction}/{claim.certainty}] "
            f"({claim.topic}) {claim.statement}{population}.{effect}"
        )
    if appraisal.limitations:
        lines.append(f"  limitations: {'; '.join(appraisal.limitations[:4])}")
    if appraisal.conflicts_of_interest.strip():
        lines.append(f"  funding/COI: {appraisal.conflicts_of_interest}")

    return "\n".join(lines)


def build_digest(
    records: list[SourceRecord],
    appraisals: dict[str, RecordAppraisal],
    metrics: dict[str, Any],
) -> str:
    """The whole corpus, plus what arithmetic already knows about it."""
    by_kind: dict[str, list[str]] = {}
    ordering = {"paper": 0, "trial": 1, "regulatory": 2, "safety": 3,
                "compound": 4, "document": 5, "news": 6}

    for record in sorted(
        records,
        key=lambda r: (
            ordering.get(r.kind, 9),
            -(appraisals[r.sid].relevance if r.sid in appraisals else 0),
        ),
    ):
        by_kind.setdefault(record.kind, []).append(
            _render_appraised(record, appraisals.get(record.sid))
        )

    sections = [
        corpus_block(by_kind.get("paper", []), "Published literature"),
        corpus_block(by_kind.get("trial", []), "Clinical trial registry records"),
        corpus_block(by_kind.get("regulatory", []), "Regulatory records and approved labels"),
        corpus_block(by_kind.get("safety", []), "Recalls and enforcement actions"),
        corpus_block(by_kind.get("compound", []), "Curated pharmacology"),
        corpus_block(
            by_kind.get("document", []),
            "User-supplied documents (not peer reviewed - weight accordingly)",
        ),
        corpus_block(by_kind.get("news", []), "Recent developments from the open web"),
    ]

    sections.append(_render_analytics(metrics))
    return "\n".join(sections)


def _render_analytics(metrics: dict[str, Any]) -> str:
    """Hand the model the numbers rather than letting it estimate them."""
    lit = metrics.get("literature", {})
    trials = metrics.get("trials", {})
    reg = metrics.get("regulatory", {})
    claims = metrics.get("claims", {})
    quality = metrics.get("quality", {})
    consensus = metrics.get("consensus", {})
    coverage = metrics.get("coverage", {})
    faers = reg.get("faers", {})

    lines = ["## Computed analytics (already calculated - do not recompute, do cite in prose)", ""]

    lines.append(
        f"Corpus: {metrics['totals']['records']} records "
        f"({lit.get('count', 0)} papers, {trials.get('count', 0)} trials, "
        f"{reg.get('count', 0)} regulatory), spanning {metrics.get('year_span')}. "
        f"{claims.get('count', 0)} structured claims extracted."
    )
    lines.append(
        f"Evidence Quality Index: {quality.get('score', 0)}/100 "
        f"(components: " + ", ".join(
            f"{name} {detail['points']}/{detail['weight']}"
            for name, detail in (quality.get("components") or {}).items()
        ) + ")."
    )
    lines.append(
        f"Consensus Index: {consensus.get('score', 0)}/100 across "
        f"{consensus.get('topics_assessed', 0)} topics with at least two directional claims."
    )

    contested = consensus.get("contested_topics") or []
    if contested:
        lines.append("")
        lines.append("TOPICS WHERE THE CORPUS DISAGREES WITH ITSELF (address each in `conflicts`):")
        for row in contested[:10]:
            lines.append(
                f"  - '{row['topic']}': {row['supports']} supporting, {row['refutes']} refuting, "
                f"{row['mixed']} neutral/mixed - {row['agreement']}% agreement"
            )
    else:
        lines.append("")
        lines.append(
            "No topic has two or more directional claims pointing in opposite directions. "
            "If you report no conflicts, say explicitly that the corpus is internally consistent "
            "and name what that consistency does and does not prove."
        )

    thin = coverage.get("thin_dimensions") or []
    if thin:
        lines.append("")
        lines.append("DIMENSIONS WITH ALMOST NO EVIDENCE (strong candidates for `gaps`):")
        for row in thin:
            lines.append(f"  - {row['dimension']}: {row['claims']} claim(s) in the whole corpus")

    if trials.get("count"):
        lines.append("")
        lines.append(
            f"Trial landscape: highest phase {trials.get('highest_phase')}; "
            f"{trials.get('active', 0)} active, {trials.get('completed', 0)} completed, "
            f"{trials.get('stopped', 0)} terminated/withdrawn/suspended; "
            f"{trials.get('with_results', 0)} with posted results; "
            f"{trials.get('total_enrollment', 0):,} participants across retrieved trials; "
            f"{trials.get('industry_share', 0)}% industry-sponsored. "
            f"Registry reports {trials.get('registry_total', 0)} matching studies in total."
        )
        largest = trials.get("largest_trial") or {}
        if largest.get("enrollment"):
            lines.append(
                f"Largest retrieved trial: {largest.get('nct')}, {largest.get('phase')}, "
                f"n={largest.get('enrollment'):,}."
            )

    if reg.get("count"):
        lines.append("")
        lines.append(
            f"Regulatory: applications {', '.join(reg.get('applications') or []) or 'none'}; "
            f"{reg.get('approval_actions', 0)} approval actions "
            f"(first {reg.get('first_approval') or 'n/a'}, latest {reg.get('latest_approval') or 'n/a'}); "
            f"{reg.get('priority_reviews', 0)} priority reviews; "
            f"boxed warning: {'yes' if reg.get('has_boxed_warning') else 'not found'}; "
            f"{reg.get('recalls', 0)} recall records."
        )

    if faers.get("top_reactions"):
        lines.append("")
        lines.append(
            "FAERS spontaneous reports (REPORTING VOLUME, NOT INCIDENCE - no denominator, "
            f"voluntary reporting, no causation): {faers.get('serious_reports', 0):,} reports "
            "flagged serious. Most-reported terms: "
            + ", ".join(
                f"{r['term']} ({r['count']:,})" for r in faers["top_reactions"][:10]
            )
            + "."
        )

    if lit.get("design_mix"):
        lines.append("")
        lines.append(
            "Literature design mix: "
            + ", ".join(f"{d['design']} {d['count']}" for d in lit["design_mix"])
            + f". {lit.get('recent_share', 0)}% published in the last three years."
        )

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #

_TASK = """\
TASK: Produce the complete research report for the question below, as structured data.

This is the analytical deliverable. Work through it field by field.

`summary.verdict` - one sentence a reader could quote. It must commit to something. \
"Evidence on X is mixed" is not a verdict; "Three randomised trials establish a \
20-25% relative reduction in MACE, but none enrolled patients over 75" is.

`summary.narrative` - 3-5 paragraphs of flowing prose. No bullets, no headings, no \
markdown. Quantify. Name the studies that carry the argument by handle. This is the \
section a busy expert reads instead of the rest, so it must stand alone: what is \
established, on what evidence, what remains open, and what would change the picture.

`summary.consensus` and `summary.maturity` - reuse the computed Consensus Index \
verbatim for `consensus`. For `maturity`, judge bench-to-practice progress from the \
trial phases, approval record and guideline presence in the corpus: preclinical only \
is under 20; first-in-human is 20-40; randomised trials running is 40-60; approved \
with a developing evidence base is 60-85; established standard of care with \
long-term data is above 85.

`key_findings` - 5 to 9, ordered by how much they should change a reader's mind, not \
by topic. Each `headline` is a claim with content, and each `detail` carries the \
numbers. `strength` reflects the evidence behind that specific finding, not the \
corpus average: `strong` needs consistent Tier 1 evidence, `preliminary` means \
single-study or preclinical. Do not pad to nine.

`comparison` - build the matrix only if the corpus actually supports comparing two or \
more named options. If it does not, return an empty `entities` list and an empty \
`rows` list, and say why in `bottom_line`. When you do build it, use 5-8 criteria that \
a formulary committee would care about - primary efficacy, key safety signal, \
administration burden, evidence quality, population studied, open questions - and \
write `Not established` in any cell the corpus does not support. Never infer a \
comparator's value from the other column.

`timeline` - 6 to 15 events, chronological, drawn from approval dates, trial start \
and completion dates, and landmark publications that are actually in the corpus. \
Every event needs a real date from a real record.

`conflicts` - work through every topic listed as contested in the computed analytics, \
plus any further genuine contradiction you can see. A conflict is two sources that \
cannot both be right about the same question in the same population - not two studies \
of different things, and not a study that simply reports less benefit than another. \
`assessment` must take a position: name which side the stronger evidence favours and \
why, or state plainly that it is unresolved and what would resolve it. \
`likely_explanation` should be a specific methodological difference - dose, endpoint \
definition, follow-up length, population risk, funding source - not "further research \
is needed". If there is genuinely no conflict, return an empty list; do not manufacture \
one.

`gaps` - 4 to 8. Start from the thin dimensions in the computed analytics, then add \
what you can see is missing that counting cannot detect: an unstudied population, an \
endpoint nobody measured, a duration nobody reached, a comparison nobody ran, a \
mechanism nobody tested. `what_would_answer_it` must describe a study specifically \
enough to be costed - design, population, sample size order of magnitude, endpoint, \
duration. `evidence_absent` names what the corpus conspicuously lacks. A gap that \
would not change practice if closed is not `critical`.

`safety_signals` - drawn from labels, boxed warnings, trial adverse events and \
reported-event data. Where a signal rests on spontaneous reporting counts, say so in \
`context` and do not phrase it as a rate.

`regulatory_status` and `mechanism` - one substantial paragraph each, prose, cited. If \
the corpus has no regulatory records, say that rather than describing regulatory \
status in general terms."""


async def synthesize(
    brain: Brain,
    plan: ResearchPlan,
    digest: str,
    *,
    query: str,
    model: str = SYNTHESIS_MODEL,
    effort: str = "high",
    on_thinking: ThinkingSink = None,
) -> SynthesisBundle:
    handles = re.findall(r"^\[([A-Z]+\d+)\]", digest, flags=re.MULTILINE)

    user = "\n\n".join([
        _TASK,
        f"ORIGINAL QUESTION AS ASKED: {query.strip()}",
        f"INTERPRETED AS: {plan.interpretation}",
        f"QUESTION TYPE: {plan.intent}",
        f"ENTITIES IN SCOPE: {', '.join(e.name for e in plan.primary_entities) or 'unspecified'}",
        f"COMPARATORS THE READER EXPECTS: {', '.join(e.name for e in plan.comparators) or 'none'}",
        f"OUTCOMES THAT MATTER: {', '.join(plan.key_outcomes) or 'unspecified'}",
        f"EXPERT OPEN QUESTIONS FROM PLANNING: {'; '.join(plan.open_questions) or 'none'}",
        "VALID CITATION HANDLES - using any handle not in this list is an error:\n"
        + ", ".join(f"[{h}]" for h in handles),
        "=" * 70,
        "CORPUS",
        "=" * 70,
        digest,
    ])

    return await brain.structured(
        model=model,
        schema_model=SynthesisBundle,
        system=SYSTEM,
        user=user,
        max_tokens=MAX_TOKENS_SYNTHESIS,
        effort=effort,
        on_thinking=on_thinking,
    )


# --------------------------------------------------------------------------- #
# Citation audit
# --------------------------------------------------------------------------- #

_HANDLE_RE = re.compile(r"\[([A-Za-z]+\d+)\]")


def audit_citations(bundle: SynthesisBundle, valid: set[str]) -> list[str]:
    """Strip every citation handle the corpus does not contain.

    This is the backstop on rule 2 of the prompt contract. A handle that does not
    resolve to a retrieved record is removed from the structured citation lists and
    de-linked in prose, and each removal is reported. In practice this fires rarely,
    which is the point: it is cheap, and it means a hallucinated reference can never
    reach the rendered report or an export.
    """
    problems: list[str] = []

    def clean_list(handles: list[str], where: str) -> list[str]:
        kept: list[str] = []
        for handle in handles:
            normalised = handle.strip().strip("[]").upper()
            if normalised in valid:
                if normalised not in kept:
                    kept.append(normalised)
            else:
                problems.append(f"{where}: dropped unknown citation [{handle}]")
        return kept

    def clean_prose(text: str, where: str) -> str:
        # 【S4】 and ［S4］ are citations too - normalise them so they are audited.
        text = re.sub(r"[【［〚]\s*([A-Za-z]{1,2}\d{1,4})\s*[】］〛]", r"[\1]", text)

        def replace(match: re.Match[str]) -> str:
            handle = match.group(1).upper()
            if handle in valid:
                return f"[{handle}]"
            problems.append(f"{where}: removed unknown inline citation [{match.group(1)}]")
            return ""

        cleaned = _HANDLE_RE.sub(replace, text)
        # Tidy up after a removal without touching paragraph structure: the
        # executive summary is multi-paragraph prose, so collapsing every run of
        # whitespace would flatten it into one block.
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"[ \t]+([.,;:!?)])", r"\1", cleaned)
        cleaned = re.sub(r"\(\s+", "(", cleaned)
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    for i, finding in enumerate(bundle.key_findings, 1):
        finding.citations = clean_list(finding.citations, f"key finding {i}")
        finding.detail = clean_prose(finding.detail, f"key finding {i}")

    for row in bundle.comparison.rows:
        for cell in row.cells:
            cell.citations = clean_list(cell.citations, f"comparison '{row.criterion}'")

    for event in bundle.timeline:
        event.citations = clean_list(event.citations, f"timeline '{event.label}'")

    for conflict in bundle.conflicts:
        conflict.citations_a = clean_list(conflict.citations_a, f"conflict '{conflict.topic}' side A")
        conflict.citations_b = clean_list(conflict.citations_b, f"conflict '{conflict.topic}' side B")

    for signal in bundle.safety_signals:
        signal.citations = clean_list(signal.citations, f"safety signal '{signal.event}'")

    bundle.summary.narrative = clean_prose(bundle.summary.narrative, "executive summary")
    bundle.summary.verdict = clean_prose(bundle.summary.verdict, "verdict")
    bundle.regulatory_status = clean_prose(bundle.regulatory_status, "regulatory status")
    bundle.mechanism = clean_prose(bundle.mechanism, "mechanism")

    # A finding with no surviving citation is unsupported and must not be shown
    # as evidence-backed.
    uncited = [f.headline for f in bundle.key_findings if not f.citations]
    if uncited:
        problems.append(
            f"{len(uncited)} key finding(s) carried no resolvable citation and were "
            "marked unsupported"
        )

    return problems


# --------------------------------------------------------------------------- #
# Number check
# --------------------------------------------------------------------------- #

# Decimals ("0.80", "1.5"), percentages ("20%", "6.5 %") and numbers of 50 or more
# (sample sizes, event counts). Small bare integers are skipped: "three trials" is a
# count of the corpus, not a figure any one source states.
_NUMBER_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%?)")


def _figures(text: str) -> set[str]:
    found: set[str] = set()
    for raw, percent in _NUMBER_RE.findall(text or ""):
        value = raw.replace(",", "")
        if percent or "." in value or float(value) >= 50:
            found.add(value.rstrip("0").rstrip(".") if "." in value else value)
    return found


def check_numbers(
    bundle: SynthesisBundle,
    records: dict[str, SourceRecord],
    appraisals: dict[str, RecordAppraisal],
) -> dict[str, Any]:
    """Does every figure in a key finding appear in a source it cites?

    Citations prove a finding points at real records; this checks the numbers in it
    came from them. A figure that appears in none of the cited records' text or
    structured claims is flagged on the finding - it may be a fair derivation (a
    difference of two reported rates), but the reader should know it is not quoted.
    Returns {"findings": {index: {"checked", "unverified"}}, "checked", "unverified"}.
    """
    results: dict[int, dict[str, Any]] = {}
    total_checked = total_unverified = 0
    for index, finding in enumerate(bundle.key_findings, 1):
        figures = _figures(f"{finding.headline} {finding.detail}")
        if not figures or not finding.citations:
            continue
        haystack: set[str] = set()
        for sid in finding.citations:
            record = records.get(sid)
            if record is None:
                continue
            haystack |= _figures(f"{record.title} {record.snippet} {record.date} {record.year or ''}")
            if record.sample_size:
                haystack.add(str(record.sample_size))
            appraisal = appraisals.get(sid)
            if appraisal is not None:
                haystack |= _figures(appraisal.summary)
                for claim in appraisal.claims:
                    haystack |= _figures(f"{claim.statement} {claim.effect} {claim.population}")
        unverified = sorted(figures - haystack, key=lambda v: float(v))
        results[index] = {"checked": len(figures), "unverified": unverified}
        total_checked += len(figures)
        total_unverified += len(unverified)
    return {"findings": results, "checked": total_checked, "unverified": total_unverified}
