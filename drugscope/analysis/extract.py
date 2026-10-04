"""Stage 2 - appraise each retrieved record on its own.

One call per record, run concurrently. That is more expensive than stuffing the
whole corpus into a single call, and it buys three things worth paying for:

* **Attention.** A 60-paper corpus in one prompt gets a skim. One abstract per call
  gets a read, so effect sizes and populations actually come back.
* **Isolation.** A model that has just read a glowing industry-sponsored trial does
  not carry that impression into the next abstract. Each record is judged on its
  own contents.
* **Attributable claims.** Because the call only ever saw one record, every claim it
  returns is attributable to that record by construction - there is no way for a
  number from paper 12 to end up cited as paper 30.

The controlled topic vocabulary from the plan is passed into every call so claims
about the same thing land under the same label. That convergence is what makes
conflict detection and the consensus index possible - without it, twenty papers
produce twenty topic names and nothing ever collides.
"""

from __future__ import annotations

from typing import Any

from ..config import MAX_TOKENS_EXTRACTION
from ..llm import Brain, gather_capped
from ..models import AppraisalBatch, RecordAppraisal, ResearchPlan, SourceRecord
from .prompts import SYSTEM

_KIND_GUIDANCE = {
    "paper": (
        "This is a journal record. Classify the design from what the abstract "
        "reports, not from what it claims to be - an article calling itself a "
        "'real-world study' with no control group is a cross-sectional or cohort "
        "record, not a trial. If it is animal, cell-line or in-silico work, the "
        "design is `preclinical` regardless of how clinically it is framed."
    ),
    "trial": (
        "This is a trial registry record, so it describes a study's *design and "
        "status*, not its results - unless results are explicitly posted. Claims "
        "you extract should be about what is being tested, in whom, at what phase, "
        "and whether it completed, stopped or is still running. Do not report an "
        "outcome the registry has not posted; a registered primary outcome measure "
        "is a plan, not a finding."
    ),
    "regulatory": (
        "This is regulatory text - an approval record or approved label. Treat it as "
        "authoritative on regulatory status, indication wording, boxed warnings and "
        "approval dates, and as secondary on efficacy magnitude. Design is "
        "`guideline`. A boxed warning is a high-certainty safety claim."
    ),
    "safety": (
        "This is a recall or enforcement record. It speaks to manufacturing or "
        "product-quality problems, which is a different thing from the drug's "
        "pharmacological safety. Keep that distinction in the claim wording."
    ),
    "compound": (
        "This is curated pharmacology - mechanism, target, development stage, class. "
        "Claims here are `mechanism` or `pharmacology`, and their direction is "
        "usually `neutral`: knowing a molecule is a GLP-1 agonist does not by itself "
        "support or refute clinical benefit."
    ),
    "document": (
        "This is an excerpt from a document the user uploaded, not a database record. "
        "It has no peer review behind it and may be an internal memo, a protocol or a "
        "draft. Appraise what it actually claims, keep `certainty` at `moderate` or "
        "below unless it reports a completed study with numbers, and never treat it as "
        "equivalent to a published trial."
    ),
    "news": (
        "This is a recent development gathered from the open web. It is the weakest "
        "evidence in the corpus. Certainty is `low` unless it reports a named, dated "
        "regulatory action or a published trial result."
    ),
}

_TASK = """\
TASK: Appraise the single record below and return it as structured data.

How to do this well:

- `relevance` is about THIS research question, not the record's general importance. \
  A landmark trial in the wrong population scores low. Be willing to use the whole \
  0-100 range; if most of your scores land between 60 and 80 you are not \
  discriminating.

- `claims` are the substance of this stage. Extract 1-6 of them. Each must be a \
  standalone sentence that would still make sense quoted on its own, and each must \
  carry the record's own numbers where it reports them - "HbA1c fell 1.5% versus \
  0.4% with placebo at 56 weeks" beats "improved glycaemic control" every time. Do \
  not extract background statements the record is merely repeating from elsewhere.

- `direction` is judged against the intervention being effective or beneficial FOR \
  THAT CLAIM'S TOPIC. A finding that a drug causes more nausea than placebo is a \
  `safety` claim whose direction is `refutes` (it counts against the intervention). \
  A finding of no difference is `neutral`. A finding that splits by subgroup is \
  `mixed`.

- `topic` must be reused verbatim across records wherever they discuss the same \
  thing, because these labels are what group claims for conflict detection. Prefer a \
  label from the controlled vocabulary below. Only mint a new one when nothing fits, \
  and keep it 2-5 words, lower case.

- `limitations` should be concrete and specific to this record: "single-centre, 40 \
  patients, no blinding, 12-week follow-up" - not "more research is needed".

- `sample_size` is the number analysed. Use -1 when the record does not state one, \
  and never infer it from context."""


def render_record(record: SourceRecord) -> str:
    """The prompt view of one record: metadata the model needs, nothing more."""
    lines = [
        f"HANDLE: [{record.sid}]",
        f"TYPE: {record.kind}",
        f"TITLE: {record.title}",
    ]
    if record.venue:
        lines.append(f"PUBLISHED IN: {record.venue}")
    if record.date or record.year:
        lines.append(f"DATE: {record.date or record.year}")
    if record.authors:
        lines.append(f"AUTHORS: {', '.join(record.authors[:6])}")
    if record.identifiers:
        lines.append(
            "IDENTIFIERS: " + ", ".join(f"{k}={v}" for k, v in record.identifiers.items())
        )
    lines.append(f"INDEXED DESIGN HINT: {record.design} (verify against the content)")
    if record.meta.get("pub_types"):
        lines.append(f"PUBLICATION TYPES: {', '.join(record.meta['pub_types'][:6])}")
    if record.meta.get("mesh"):
        lines.append(f"MeSH: {', '.join(record.meta['mesh'][:10])}")
    if record.kind == "trial":
        meta = record.meta
        lines.append(
            f"REGISTRY FIELDS: phase={meta.get('phase')}; status={meta.get('status')}; "
            f"enrolment={meta.get('enrollment')}; allocation={meta.get('allocation')}; "
            f"masking={meta.get('masking')}; results_posted={meta.get('has_results')}"
        )
    lines.append("")
    lines.append("CONTENT:")
    lines.append(record.snippet or "(no abstract or summary available)")
    return "\n".join(lines)


async def appraise_record(
    brain: Brain,
    record: SourceRecord,
    plan: ResearchPlan,
    *,
    model: str,
    effort: str,
) -> RecordAppraisal:
    vocabulary = sorted({
        *(o.strip().lower() for o in plan.key_outcomes if o.strip()),
        *(c.strip().lower() for c in plan.conditions if c.strip()),
    })

    user = "\n\n".join([
        _TASK,
        f"RESEARCH QUESTION: {plan.interpretation}",
        f"OUTCOMES THAT MATTER: {', '.join(plan.key_outcomes) or 'not specified'}",
        "CONTROLLED TOPIC VOCABULARY (prefer these labels verbatim):\n"
        + ("\n".join(f"- {v}" for v in vocabulary) if vocabulary else "(none supplied)"),
        f"RECORD-TYPE GUIDANCE: {_KIND_GUIDANCE.get(record.kind, '')}",
        "--- RECORD ---",
        render_record(record),
    ])

    return await brain.structured(
        model=model,
        schema_model=RecordAppraisal,
        system=SYSTEM,
        user=user,
        max_tokens=MAX_TOKENS_EXTRACTION,
        effort=effort,
    )


async def appraise_batch(
    brain: Brain,
    records: list[SourceRecord],
    plan: ResearchPlan,
    *,
    model: str,
    effort: str,
) -> dict[str, RecordAppraisal]:
    """Appraise several records in one call.

    Used by rate-limited backends where one call per record would exhaust a daily
    quota in a single run. Quality is lower than the one-record path - that is the
    trade being made - so results are mapped back by the handle the model echoes,
    and any handle it invents or omits is simply dropped.
    """
    vocabulary = sorted({
        *(o.strip().lower() for o in plan.key_outcomes if o.strip()),
        *(c.strip().lower() for c in plan.conditions if c.strip()),
    })

    user = "\n\n".join([
        _TASK,
        f"You are appraising {len(records)} records in this one call. Return exactly "
        f"one appraisal per record, each naming its handle in the `handle` field. "
        f"Appraise each record only on its own contents - do not let one record's "
        f"findings colour another's.",
        f"RESEARCH QUESTION: {plan.interpretation}",
        f"OUTCOMES THAT MATTER: {', '.join(plan.key_outcomes) or 'not specified'}",
        "CONTROLLED TOPIC VOCABULARY (prefer these labels verbatim):\n"
        + ("\n".join(f"- {v}" for v in vocabulary) if vocabulary else "(none supplied)"),
        "--- RECORDS ---",
        "\n\n---\n\n".join(render_record(record) for record in records),
    ])

    batch = await brain.structured(
        model=model,
        schema_model=AppraisalBatch,
        system=SYSTEM,
        user=user,
        max_tokens=MAX_TOKENS_EXTRACTION * max(2, len(records) // 2),
        effort=effort,
    )

    valid = {record.sid for record in records}
    out: dict[str, RecordAppraisal] = {}
    for item in batch.appraisals:
        handle = item.handle.strip().strip("[]").upper()
        if handle not in valid or handle in out:
            continue
        out[handle] = RecordAppraisal(
            design=item.design,
            sample_size=item.sample_size,
            relevance=item.relevance,
            summary=item.summary,
            claims=item.claims,
            limitations=item.limitations,
            conflicts_of_interest=item.conflicts_of_interest,
        )
    return out


def _absorb(
    record: SourceRecord,
    appraisal: RecordAppraisal,
    appraisals: dict[str, RecordAppraisal],
) -> None:
    # Trust the registry's own enrolment figure over a model's reading.
    if record.kind == "trial" and record.sample_size:
        appraisal.sample_size = record.sample_size
    appraisals[record.sid] = appraisal
    record.design = appraisal.design
    if appraisal.sample_size and appraisal.sample_size > 0:
        record.sample_size = appraisal.sample_size


async def appraise_all(
    brain: Brain,
    records: list[SourceRecord],
    plan: ResearchPlan,
    *,
    model: str,
    effort: str,
    on_progress: Any = None,
    batch_size: int = 1,
) -> tuple[dict[str, RecordAppraisal], list[str]]:
    """Appraise every record concurrently.

    `batch_size` comes from the backend: 1 on Anthropic, where one call per record
    buys undivided attention, and larger on rate-limited gateways where call count
    is the binding constraint.

    Returns the appraisals that succeeded plus a warning per record that did not. A
    failed appraisal is never fatal: the record stays in the corpus and remains
    citable, it simply contributes no structured claims.
    """
    appraisals: dict[str, RecordAppraisal] = {}
    warnings: list[str] = []

    if batch_size <= 1:
        results = await gather_capped(
            [
                appraise_record(brain, record, plan, model=model, effort=effort)
                for record in records
            ],
            on_done=on_progress,
        )
        for record, result in zip(records, results):
            if isinstance(result, RecordAppraisal):
                _absorb(record, result, appraisals)
            else:
                warnings.append(
                    f"[{record.sid}] could not be appraised ({type(result).__name__}); "
                    "it remains in the corpus but contributes no structured claims"
                )
        return appraisals, warnings

    groups = [records[i : i + batch_size] for i in range(0, len(records), batch_size)]
    results = await gather_capped(
        [appraise_batch(brain, group, plan, model=model, effort=effort) for group in groups],
        on_done=(lambda done, total: on_progress(min(done * batch_size, len(records)), len(records)))
        if on_progress else None,
    )

    for group, result in zip(groups, results):
        if isinstance(result, dict):
            for record in group:
                appraisal = result.get(record.sid)
                if appraisal is not None:
                    _absorb(record, appraisal, appraisals)
                else:
                    warnings.append(
                        f"[{record.sid}] was omitted from its batch appraisal; it remains "
                        "citable but contributes no structured claims"
                    )
        else:
            warnings.append(
                f"a batch of {len(group)} records could not be appraised "
                f"({type(result).__name__}); they remain citable but contribute no claims"
            )

    return appraisals, warnings
