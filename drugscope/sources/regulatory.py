"""Regulatory and post-market safety: the openFDA family of endpoints.

Four distinct streams, deliberately kept apart because they answer different
questions:

* **Drugs@FDA** (`/drug/drugsfda`) - the approval record. Application numbers,
  sponsors, marketing status, and the full submission history that the
  development timeline is built from.
* **Labels** (`/drug/label`) - the prescribing information as approved: boxed
  warnings, indications, mechanism of action.
* **FAERS** (`/drug/event`) - spontaneous adverse-event reports, pulled as
  aggregate counts rather than individual reports.
* **Enforcement** (`/drug/enforcement`) - recalls and their classification.

A hard caveat that the UI repeats and the report carries: FAERS counts are
**reporting volume, not incidence**. There is no denominator, reporting is
voluntary, and a report is not a finding of causation. Treating these counts as
rates is the single most common way to misread this data.
"""

from __future__ import annotations

from typing import Any

from ..models import SourceRecord
from .base import Fetcher, clean_text, first_year

BASE = "https://api.fda.gov/drug"

FAERS_CAVEAT = (
    "FAERS counts are spontaneous reporting volume, not incidence: there is no "
    "exposure denominator, reporting is voluntary and uneven, and a report does "
    "not establish causation."
)

_SUBMISSION_STATUS = {"AP": "Approved", "TA": "Tentative approval", "CR": "Complete response"}
# FAERS reaction-outcome codes, per the openFDA data dictionary.
_OUTCOME_LABELS = {
    "1": "Recovered / resolved",
    "2": "Recovering / resolving",
    "3": "Not recovered",
    "4": "Recovered with sequelae",
    "5": "Fatal",
    "6": "Unknown",
}

_RECALL_SEVERITY = {
    "Class I": "critical",
    "Class II": "serious",
    "Class III": "moderate",
}


def _fda_name_query(name: str) -> str:
    """Name query for the label endpoint, where openfda.* is well populated."""
    safe = name.replace('"', "").strip()
    return (
        f'openfda.generic_name:"{safe}" '
        f'OR openfda.brand_name:"{safe}" '
        f'OR openfda.substance_name:"{safe}"'
    )


def _drugsfda_name_query(name: str) -> str:
    """Name query for Drugs@FDA.

    `openfda.*` is only sparsely populated on this endpoint - searching it alone
    finds one application for semaglutide and misses the others. The
    per-product `active_ingredients.name` and `brand_name` fields are the
    reliable ones, so all of them are ORed together.
    """
    safe = name.replace('"', "").strip()
    return (
        f'products.active_ingredients.name:"{safe}" '
        f'OR products.brand_name:"{safe}" '
        f'OR openfda.generic_name:"{safe}" '
        f'OR openfda.brand_name:"{safe}" '
        f'OR openfda.substance_name:"{safe}"'
    )


# --------------------------------------------------------------------------- #
# Drugs@FDA - approvals
# --------------------------------------------------------------------------- #

async def search_approvals(fetcher: Fetcher, name: str, *, limit: int = 8) -> list[SourceRecord]:
    payload = await fetcher.get(
        f"{BASE}/drugsfda.json",
        params={"search": _drugsfda_name_query(name), "limit": min(limit, 25)},
        label="Drugs@FDA",
    )
    if not payload:
        return []

    records: list[SourceRecord] = []
    for entry in (payload.get("results") or [])[:limit]:
        app_no = clean_text(entry.get("application_number"), 30)
        sponsor = clean_text(entry.get("sponsor_name"), 160)
        products = entry.get("products") or []

        brands = sorted({clean_text(p.get("brand_name"), 80) for p in products if p.get("brand_name")})
        forms = sorted({clean_text(p.get("dosage_form"), 60) for p in products if p.get("dosage_form")})
        routes = sorted({clean_text(p.get("route"), 60) for p in products if p.get("route")})
        marketing = sorted({clean_text(p.get("marketing_status"), 60) for p in products if p.get("marketing_status")})
        ingredients = sorted({
            f"{clean_text(a.get('name'), 60)} {clean_text(a.get('strength'), 40)}".strip()
            for p in products
            for a in (p.get("active_ingredients") or [])
            if a.get("name")
        })

        submissions: list[dict[str, Any]] = []
        for sub in entry.get("submissions") or []:
            status_code = clean_text(sub.get("submission_status"), 8)
            submissions.append({
                "type": clean_text(sub.get("submission_type"), 30),
                "number": clean_text(sub.get("submission_number"), 12),
                "status": _SUBMISSION_STATUS.get(status_code, status_code),
                "date": clean_text(sub.get("submission_status_date"), 12),
                "priority": clean_text(sub.get("review_priority"), 40),
                "classification": clean_text(sub.get("submission_class_code_description"), 120),
            })
        submissions.sort(key=lambda s: s["date"])

        approvals = [s for s in submissions if s["status"] == "Approved" and s["date"]]
        first_approval = approvals[0]["date"] if approvals else ""
        latest_action = submissions[-1]["date"] if submissions else ""

        title = f"{brands[0] if brands else name.title()} - FDA application {app_no}"
        snippet = clean_text(" ".join(filter(None, [
            f"Application {app_no} held by {sponsor}." if sponsor else f"Application {app_no}.",
            f"Brands: {', '.join(brands[:5])}." if brands else "",
            f"Active ingredients: {', '.join(ingredients[:5])}." if ingredients else "",
            f"Dosage forms: {', '.join(forms[:5])}; routes: {', '.join(routes[:4])}." if forms or routes else "",
            f"Marketing status: {', '.join(marketing)}." if marketing else "",
            f"First recorded approval: {first_approval}." if first_approval else "",
            f"{len(approvals)} approval actions across {len(submissions)} submissions.",
            "Supplement history: " + "; ".join(
                f"{s['date']} {s['type']} {s['number']} ({s['classification'] or s['status']})"
                for s in submissions[-8:]
            ) + "." if submissions else "",
        ])), 2600)

        records.append(SourceRecord(
            sid="",
            kind="regulatory",
            title=title,
            url=f"https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={app_no.lstrip('ANDABLA')}",
            source="Drugs@FDA",
            snippet=snippet,
            date=first_approval,
            year=first_year(first_approval, latest_action),
            venue="U.S. Food and Drug Administration",
            identifiers={"fda_application": app_no},
            meta={
                "sponsor": sponsor,
                "brands": brands,
                "dosage_forms": forms,
                "routes": routes,
                "marketing_status": marketing,
                "active_ingredients": ingredients,
                "submissions": submissions,
                "approval_count": len(approvals),
                "first_approval": first_approval,
                "latest_action": latest_action,
            },
            design="guideline",
        ))

    return records


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

async def search_labels(fetcher: Fetcher, name: str, *, limit: int = 3) -> list[SourceRecord]:
    payload = await fetcher.get(
        f"{BASE}/label.json",
        params={"search": _fda_name_query(name), "limit": min(limit, 10)},
        label="FDA labels",
    )
    if not payload:
        return []

    records: list[SourceRecord] = []
    seen_brands: set[str] = set()
    for entry in (payload.get("results") or []):
        if len(records) >= limit:
            break
        openfda = entry.get("openfda") or {}
        brand = clean_text((openfda.get("brand_name") or [name])[0], 80)
        generic = clean_text((openfda.get("generic_name") or [""])[0], 80)
        manufacturer = clean_text((openfda.get("manufacturer_name") or [""])[0], 160)
        effective = clean_text(entry.get("effective_time"), 12)
        set_id = clean_text(entry.get("set_id") or (entry.get("id") if isinstance(entry.get("id"), str) else ""), 60)

        boxed = clean_text(entry.get("boxed_warning"), 1200)
        indications = clean_text(entry.get("indications_and_usage"), 1400)
        moa = clean_text(entry.get("mechanism_of_action"), 900)
        warnings = clean_text(
            entry.get("warnings_and_cautions") or entry.get("warnings"), 1200
        )
        contraindications = clean_text(entry.get("contraindications"), 700)

        snippet = clean_text(" ".join(filter(None, [
            f"BOXED WARNING: {boxed}" if boxed else "",
            f"Indications: {indications}" if indications else "",
            f"Mechanism of action: {moa}" if moa else "",
            f"Warnings and cautions: {warnings}" if warnings else "",
            f"Contraindications: {contraindications}" if contraindications else "",
        ])), 3000)

        if not snippet:
            continue

        # One prescribing information document is repeated per repackager.
        brand_key = (brand or generic or name).lower()
        if brand_key in seen_brands:
            continue
        seen_brands.add(brand_key)

        records.append(SourceRecord(
            sid="",
            kind="regulatory",
            title=f"FDA prescribing information - {brand or generic or name}",
            url=(
                f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
                if set_id else "https://labels.fda.gov/"
            ),
            source="FDA label",
            snippet=snippet,
            date=effective,
            year=first_year(effective),
            venue="U.S. Food and Drug Administration",
            identifiers={"set_id": set_id} if set_id else {},
            meta={
                "brand": brand,
                "generic": generic,
                "manufacturer": manufacturer,
                "has_boxed_warning": bool(boxed),
                "boxed_warning": boxed,
                "mechanism_of_action": moa,
            },
            design="guideline",
        ))

    return records


# --------------------------------------------------------------------------- #
# FAERS - post-market safety reporting
# --------------------------------------------------------------------------- #

async def adverse_event_profile(fetcher: Fetcher, name: str, *, top: int = 20) -> dict[str, Any]:
    """Aggregate FAERS counts. Returns {} when the drug has no reports."""
    safe = name.replace('"', "").strip()
    drug_query = (
        f'patient.drug.medicinalproduct:"{safe}" '
        f'OR patient.drug.openfda.generic_name:"{safe}" '
        f'OR patient.drug.openfda.brand_name:"{safe}"'
    )

    reactions = await fetcher.get(
        f"{BASE}/event.json",
        params={
            "search": drug_query,
            "count": "patient.reaction.reactionmeddrapt.exact",
            "limit": min(top, 50),
        },
        label="openFDA FAERS",
    )
    if not reactions or not reactions.get("results"):
        return {}

    serious = await fetcher.get(
        f"{BASE}/event.json",
        params={"search": f"({drug_query}) AND serious:1", "limit": 1},
        label="openFDA FAERS (serious)",
    )
    outcomes = await fetcher.get(
        f"{BASE}/event.json",
        params={
            "search": drug_query,
            "count": "patient.reaction.reactionoutcome",
            "limit": 10,
        },
        label="openFDA FAERS (outcomes)",
    )

    top_reactions = [
        {"term": clean_text(r.get("term"), 120).title(), "count": int(r.get("count") or 0)}
        for r in reactions["results"][:top]
    ]
    serious_total = int(((serious or {}).get("meta") or {}).get("results", {}).get("total") or 0)

    return {
        "top_reactions": top_reactions,
        "reported_total": sum(r["count"] for r in top_reactions),
        "serious_reports": serious_total,
        "outcome_mix": [
            {
                "code": clean_text(o.get("term"), 8),
                "label": _OUTCOME_LABELS.get(clean_text(o.get("term"), 8), "Unspecified"),
                "count": int(o.get("count") or 0),
            }
            for o in ((outcomes or {}).get("results") or [])
        ],
        "caveat": FAERS_CAVEAT,
    }


async def search_recalls(fetcher: Fetcher, name: str, *, limit: int = 6) -> list[SourceRecord]:
    safe = name.replace('"', "").strip()
    payload = await fetcher.get(
        f"{BASE}/enforcement.json",
        params={
            "search": f'product_description:"{safe}" OR openfda.generic_name:"{safe}"',
            "limit": min(limit, 20),
        },
        label="FDA enforcement",
    )
    if not payload:
        return []

    records: list[SourceRecord] = []
    for entry in (payload.get("results") or [])[:limit]:
        classification = clean_text(entry.get("classification"), 20)
        reason = clean_text(entry.get("reason_for_recall"), 700)
        date = clean_text(entry.get("recall_initiation_date"), 12)
        firm = clean_text(entry.get("recalling_firm"), 160)
        number = clean_text(entry.get("recall_number"), 40)

        records.append(SourceRecord(
            sid="",
            kind="safety",
            title=f"{classification or 'Recall'} - {clean_text(entry.get('product_description'), 120)}",
            url="https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts",
            source="FDA enforcement",
            snippet=clean_text(
                f"{classification} recall initiated {date} by {firm}. Reason: {reason} "
                f"Status: {clean_text(entry.get('status'), 40)}. "
                f"Distribution: {clean_text(entry.get('distribution_pattern'), 300)}",
                1800,
            ),
            date=date,
            year=first_year(date),
            venue="U.S. Food and Drug Administration",
            identifiers={"recall_number": number} if number else {},
            meta={
                "classification": classification,
                "severity": _RECALL_SEVERITY.get(classification, "moderate"),
                "firm": firm,
                "status": clean_text(entry.get("status"), 40),
            },
            design="other",
        ))

    return records
