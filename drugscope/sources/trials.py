"""Clinical trials: the ClinicalTrials.gov v2 API.

A trial record carries far more structure than a paper, and that structure is what
the development-tracking and competitive-landscape views are built from - phase,
status, enrolment, sponsor class, start and completion dates, whether results have
been posted. All of it is kept in `SourceRecord.meta` so the analysis layer reads
the same numbers the charts do.
"""

from __future__ import annotations

from typing import Any

from ..models import SourceRecord
from .base import Fetcher, clean_text, first_year

API = "https://clinicaltrials.gov/api/v2/studies"

PHASE_LABELS = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    "NA": "Not applicable",
}
PHASE_ORDER = ["Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4", "Not applicable"]
PHASE_RANK = {label: i for i, label in enumerate(PHASE_ORDER)}

STATUS_LABELS = {
    "NOT_YET_RECRUITING": "Not yet recruiting",
    "RECRUITING": "Recruiting",
    "ENROLLING_BY_INVITATION": "Enrolling by invitation",
    "ACTIVE_NOT_RECRUITING": "Active, not recruiting",
    "SUSPENDED": "Suspended",
    "TERMINATED": "Terminated",
    "WITHDRAWN": "Withdrawn",
    "COMPLETED": "Completed",
    "UNKNOWN": "Unknown status",
}
ACTIVE_STATUSES = {"Recruiting", "Not yet recruiting", "Enrolling by invitation", "Active, not recruiting"}
STOPPED_STATUSES = {"Terminated", "Withdrawn", "Suspended"}


def _phase_label(phases: list[str]) -> str:
    """Collapse the API's phase list into one display label."""
    if not phases:
        return "Not applicable"
    mapped = sorted(
        {PHASE_LABELS.get(p, p.title()) for p in phases},
        key=lambda x: PHASE_RANK.get(x, 99),
    )
    if len(mapped) == 1:
        return mapped[0]
    # A study registered as both Phase 2 and Phase 3 is a seamless 2/3 design.
    short = [m.replace("Phase ", "") for m in mapped if m.startswith("Phase")]
    return f"Phase {'/'.join(short)}" if short else mapped[0]


def _parse_study(study: dict[str, Any]) -> SourceRecord | None:
    protocol = study.get("protocolSection") or {}
    ident = protocol.get("identificationModule") or {}
    status_mod = protocol.get("statusModule") or {}
    design_mod = protocol.get("designModule") or {}
    desc_mod = protocol.get("descriptionModule") or {}
    sponsor_mod = protocol.get("sponsorCollaboratorsModule") or {}
    cond_mod = protocol.get("conditionsModule") or {}
    arms_mod = protocol.get("armsInterventionsModule") or {}
    outcomes_mod = protocol.get("outcomesModule") or {}

    nct = clean_text(ident.get("nctId"), 20)
    title = clean_text(ident.get("briefTitle") or ident.get("officialTitle"), 400)
    if not nct or not title:
        return None

    phases = design_mod.get("phases") or []
    phase = _phase_label([str(p) for p in phases])
    raw_status = str(status_mod.get("overallStatus") or "UNKNOWN")
    status = STATUS_LABELS.get(raw_status, raw_status.replace("_", " ").title())

    start = clean_text((status_mod.get("startDateStruct") or {}).get("date"), 12)
    completion = clean_text(
        (status_mod.get("primaryCompletionDateStruct") or {}).get("date")
        or (status_mod.get("completionDateStruct") or {}).get("date"),
        12,
    )

    enrollment_info = design_mod.get("enrollmentInfo") or {}
    try:
        enrollment = int(enrollment_info.get("count"))
    except (TypeError, ValueError):
        enrollment = None

    design_info = design_mod.get("designInfo") or {}
    allocation = clean_text(design_info.get("allocation"), 40)
    masking = clean_text((design_info.get("maskingInfo") or {}).get("masking"), 40)
    study_type = clean_text(design_mod.get("studyType"), 40)

    interventions = [
        clean_text(i.get("name"), 120)
        for i in (arms_mod.get("interventions") or [])
        if i.get("name")
    ][:8]
    conditions = [clean_text(c, 120) for c in (cond_mod.get("conditions") or [])][:8]
    primary_outcomes = [
        clean_text(o.get("measure"), 200)
        for o in (outcomes_mod.get("primaryOutcomes") or [])
        if o.get("measure")
    ][:5]

    lead = sponsor_mod.get("leadSponsor") or {}
    sponsor = clean_text(lead.get("name"), 160)
    sponsor_class = clean_text(lead.get("class"), 40)

    brief = clean_text(desc_mod.get("briefSummary"), 1800)
    has_results = bool(study.get("hasResults"))

    # A randomised interventional study is graded as randomised evidence; an
    # observational registry is graded as a cohort.
    if study_type.upper().startswith("INTERVENTIONAL"):
        design = "rct" if "RANDOMIZED" in allocation.upper() else "other"
    elif study_type.upper().startswith("OBSERVATIONAL"):
        design = "cohort"
    else:
        design = "other"

    snippet_bits = [
        f"{phase} {study_type.lower() or 'study'}, status: {status}.",
        f"Sponsor: {sponsor} ({sponsor_class})." if sponsor else "",
        f"Conditions: {', '.join(conditions)}." if conditions else "",
        f"Interventions: {', '.join(interventions)}." if interventions else "",
        f"Enrolment: {enrollment}." if enrollment else "",
        f"Allocation: {allocation}; masking: {masking}." if allocation or masking else "",
        f"Primary outcomes: {'; '.join(primary_outcomes)}." if primary_outcomes else "",
        f"Results posted: {'yes' if has_results else 'no'}.",
        brief,
    ]

    return SourceRecord(
        sid="",
        kind="trial",
        title=title,
        url=f"https://clinicaltrials.gov/study/{nct}",
        source="ClinicalTrials.gov",
        snippet=clean_text(" ".join(b for b in snippet_bits if b), 2600),
        date=start,
        year=first_year(start, completion),
        venue="ClinicalTrials.gov",
        identifiers={"nct": nct},
        meta={
            "phase": phase,
            "status": status,
            "is_active": status in ACTIVE_STATUSES,
            "is_stopped": status in STOPPED_STATUSES,
            "start_date": start,
            "completion_date": completion,
            "enrollment": enrollment,
            "sponsor": sponsor,
            "sponsor_class": sponsor_class,
            "study_type": study_type,
            "allocation": allocation,
            "masking": masking,
            "interventions": interventions,
            "conditions": conditions,
            "primary_outcomes": primary_outcomes,
            "has_results": has_results,
        },
        design=design,  # type: ignore[arg-type]
        sample_size=enrollment,
    )


def _weight(record: SourceRecord) -> tuple[int, int, int]:
    meta = record.meta
    phase_rank = PHASE_RANK.get(meta.get("phase", ""), 0)
    # "Not applicable" sorts last in PHASE_ORDER but means "no phase", so it must
    # not outrank Phase 4.
    if meta.get("phase") == "Not applicable":
        phase_rank = -1
    return (phase_rank, int(bool(meta.get("has_results"))), int(meta.get("enrollment") or 0))


# Share of the retrieved slots held open for trials that are still running.
ACTIVE_QUOTA = 0.3


def prioritise(records: list[SourceRecord], limit: int) -> list[SourceRecord]:
    """Rank a relevance-filtered pool by how much each trial actually weighs.

    The registry's own `@relevance` ordering is a text match, so a 68-patient
    Phase 1 bioequivalence study outranks the 17,000-patient Phase 3 outcomes trial
    that defines the drug. Relevance stays the gate - only trials the registry
    already matched are in the pool - but within it, later phase, posted results and
    larger enrolment decide what a reader sees first.

    That ordering alone has a blind spot worth guarding against: completed trials
    with posted results dominate it, so a drug with an active Phase 3 programme can
    come back looking finished. A trial landscape that shows nothing in progress is
    wrong about the thing a development-tracking question is asking, so roughly a
    third of the slots are reserved for trials that are still running, filled in the
    same weight order.
    """
    ranked = sorted(records, key=_weight, reverse=True)
    if len(ranked) <= limit:
        return ranked

    reserved = int(limit * ACTIVE_QUOTA)
    chosen = ranked[: limit - reserved]
    taken = {id(r) for r in chosen}

    active = [
        r for r in ranked
        if id(r) not in taken and r.meta.get("status") in ACTIVE_STATUSES
    ]
    chosen.extend(active[:reserved])

    # If there were not enough active trials to fill the reservation, give the
    # remaining slots back to the next-strongest trials rather than leaving them empty.
    if len(chosen) < limit:
        taken = {id(r) for r in chosen}
        chosen.extend(r for r in ranked if id(r) not in taken)

    return sorted(chosen[:limit], key=_weight, reverse=True)


async def search_trials(
    fetcher: Fetcher,
    *,
    intervention: str = "",
    condition: str = "",
    term: str = "",
    limit: int = 40,
) -> list[SourceRecord]:
    # Over-fetch so `prioritise` has a pool to rank rather than just a prefix.
    pool_size = min(max(limit * 3, 30), 200)
    params: dict[str, Any] = {
        "format": "json",
        "pageSize": pool_size,
        "countTotal": "true",
        "sort": "@relevance",
    }
    if intervention:
        params["query.intr"] = intervention
    if condition:
        params["query.cond"] = condition
    if term:
        params["query.term"] = term
    if not (intervention or condition or term):
        return []

    payload = await fetcher.get(API, params=params, label="ClinicalTrials.gov")
    if not payload:
        return []

    records: list[SourceRecord] = []
    for study in payload.get("studies") or []:
        parsed = _parse_study(study)
        if parsed is not None:
            records.append(parsed)

    total = ((payload.get("totalCount") or 0) if isinstance(payload, dict) else 0)
    ranked = prioritise(records, limit)
    for record in ranked:
        # The registry total is the honest landscape size, not what we retrieved.
        record.meta["registry_total"] = total
    return ranked
