"""Drug identity and pharmacology: RxNav/RxClass and ChEMBL.

This stream does two jobs the others cannot:

1. **Normalisation.** "Ozempic", "semaglutide" and "NN9535" are the same molecule.
   RxNav resolves a user's phrasing to an RxNorm concept and its ATC class, which
   is what lets the comparison view line up entities that the literature names
   inconsistently.
2. **Mechanism and development stage from a curated source.** ChEMBL gives a
   hand-curated mechanism of action, molecular target, and `max_phase` - a
   development-stage number that does not depend on any model's reading.
"""

from __future__ import annotations

from typing import Any

from ..models import SourceRecord
from .base import Fetcher, clean_text

RXNAV = "https://rxnav.nlm.nih.gov/REST"
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"

MAX_PHASE_LABELS = {
    4: "Approved",
    3: "Phase 3",
    2: "Phase 2",
    1: "Phase 1",
    0: "Preclinical / research",
    -1: "Unknown",
}

# RxClass relationship sources worth surfacing, mapped to a readable label.
_CLASS_TYPES = {
    "ATC1-4": "ATC class",
    "MOA": "Mechanism of action",
    "PE": "Physiologic effect",
    "EPC": "Established pharmacologic class",
    "MESHPA": "MeSH pharmacologic action",
    "DISEASE": "Indicated for",
    "CHEM": "Chemical structure class",
}


async def normalize_drug(fetcher: Fetcher, name: str) -> dict[str, Any] | None:
    """What RxNorm says a drug name means: {"ingredient", "brands"} or None.

    Questions arrive with brand names ("Ozempic"), misspellings ("metformn") and
    salts ("atorvastatin calcium"). The regulatory and pharmacology databases are
    keyed on the active ingredient, so resolving it here is what makes a free-form
    question find the same records as a carefully worded one.
    """
    found = await fetcher.get(
        f"{RXNAV}/approximateTerm.json", params={"term": name, "maxEntries": 3}, label="RxNorm match",
    )
    candidates = ((found or {}).get("approximateGroup") or {}).get("candidate") or []
    for candidate in candidates:
        rxcui = candidate.get("rxcui")
        if not rxcui:
            continue
        related = await fetcher.get(
            f"{RXNAV}/rxcui/{rxcui}/related.json", params={"tty": "IN BN"}, label="RxNorm related",
        )
        groups = ((related or {}).get("relatedGroup") or {}).get("conceptGroup") or []
        names = {g.get("tty"): [p["name"] for p in g.get("conceptProperties") or []] for g in groups}
        ingredients = names.get("IN") or []
        # A single ingredient only - a combination product is not "the drug".
        if len(ingredients) == 1:
            return {"ingredient": ingredients[0].lower(), "brands": (names.get("BN") or [])[:6]}
    return None


async def resolve_rxnorm(fetcher: Fetcher, name: str) -> dict[str, Any]:
    """Resolve a drug name to an RxNorm concept plus its class memberships."""
    profile: dict[str, Any] = {"input": name, "rxcui": "", "classes": {}}

    ids = await fetcher.get(
        f"{RXNAV}/rxcui.json",
        params={"name": name, "search": 2},
        label="RxNorm",
    )
    rxcui = ""
    if ids:
        candidates = ((ids.get("idGroup") or {}).get("rxnormId")) or []
        if candidates:
            rxcui = clean_text(candidates[0], 20)
    profile["rxcui"] = rxcui

    classes = await fetcher.get(
        f"{RXNAV}/rxclass/class/byDrugName.json",
        params={"drugName": name},
        label="RxClass",
    )
    if classes:
        grouped: dict[str, list[str]] = {}
        entries = ((classes.get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo")) or []
        for entry in entries:
            concept = entry.get("rxclassMinConceptItem") or {}
            class_type = clean_text(concept.get("classType"), 20)
            label = _CLASS_TYPES.get(class_type)
            if not label:
                continue
            value = clean_text(concept.get("className"), 140)
            bucket = grouped.setdefault(label, [])
            if value and value not in bucket:
                bucket.append(value)
        profile["classes"] = {k: v[:6] for k, v in grouped.items()}

    return profile


async def _find_molecule(fetcher: Fetcher, name: str) -> dict[str, Any]:
    """Locate the right ChEMBL molecule record for a drug name.

    Order matters. `/molecule/search` is a full-text search that happily returns a
    salt form, a research analogue or a formulation patent as the top hit, with
    most curated fields blank. An exact `pref_name` lookup gets the parent
    molecule - the one that carries `max_phase`, the mechanism and the
    indications - so it is tried first, and the search is only the fallback.
    """
    exact = await fetcher.get(
        f"{CHEMBL}/molecule.json",
        params={"pref_name__iexact": name, "limit": 5},
        label="ChEMBL exact",
    )
    candidates = list(((exact or {}).get("molecules")) or [])

    if not candidates:
        synonym = await fetcher.get(
            f"{CHEMBL}/molecule.json",
            params={"molecule_synonyms__molecule_synonym__iexact": name, "limit": 5},
            label="ChEMBL synonym",
        )
        candidates = list(((synonym or {}).get("molecules")) or [])

    if not candidates:
        search = await fetcher.get(
            f"{CHEMBL}/molecule/search",
            params={"q": name, "format": "json", "limit": 5},
            label="ChEMBL search",
        )
        candidates = list(((search or {}).get("molecules")) or [])

    if not candidates:
        return {}

    def stage(molecule: dict[str, Any]) -> float:
        try:
            return float(molecule.get("max_phase") or -1)
        except (TypeError, ValueError):
            return -1.0

    # Among equally-named candidates, prefer the most developed parent molecule.
    best = max(candidates, key=lambda m: (stage(m), bool(m.get("molecule_type"))))

    # The search endpoint returns a trimmed projection; re-read the full record.
    chembl_id = clean_text(best.get("molecule_chembl_id"), 30)
    if chembl_id and best.get("max_phase") is None:
        full = await fetcher.get(
            f"{CHEMBL}/molecule/{chembl_id}.json", label="ChEMBL molecule"
        )
        if full:
            best = full

    return best


async def chembl_profile(fetcher: Fetcher, name: str) -> dict[str, Any]:
    """Curated molecule record: development stage, mechanism, targets, indications."""
    molecule = await _find_molecule(fetcher, name)
    if not molecule:
        return {}

    chembl_id = clean_text(molecule.get("molecule_chembl_id"), 30)
    if not chembl_id:
        return {}

    try:
        max_phase = int(float(molecule.get("max_phase")))
    except (TypeError, ValueError):
        max_phase = -1

    props = molecule.get("molecule_properties") or {}
    profile: dict[str, Any] = {
        "chembl_id": chembl_id,
        "pref_name": clean_text(molecule.get("pref_name"), 120),
        "molecule_type": clean_text(molecule.get("molecule_type"), 60),
        "max_phase": max_phase,
        "max_phase_label": MAX_PHASE_LABELS.get(max_phase, "Unknown"),
        "first_approval": clean_text(molecule.get("first_approval"), 8),
        "oral": bool(molecule.get("oral")),
        "parenteral": bool(molecule.get("parenteral")),
        "black_box_warning": bool(molecule.get("black_box_warning")),
        "molecular_weight": clean_text(props.get("full_mwt"), 16),
        "mechanisms": [],
        "indications": [],
    }

    mechanisms = await fetcher.get(
        f"{CHEMBL}/mechanism",
        params={"molecule_chembl_id": chembl_id, "format": "json", "limit": 12},
        label="ChEMBL mechanism",
    )
    for mech in ((mechanisms or {}).get("mechanisms") or [])[:12]:
        profile["mechanisms"].append({
            "action": clean_text(mech.get("mechanism_of_action"), 220),
            "action_type": clean_text(mech.get("action_type"), 60),
            "target_id": clean_text(mech.get("target_chembl_id"), 30),
            "max_phase": clean_text(mech.get("max_phase"), 8),
        })

    indications = await fetcher.get(
        f"{CHEMBL}/drug_indication",
        params={"molecule_chembl_id": chembl_id, "format": "json", "limit": 25},
        label="ChEMBL indications",
    )
    seen: set[str] = set()
    for ind in ((indications or {}).get("drug_indications") or []):
        heading = clean_text(ind.get("mesh_heading"), 140)
        if not heading or heading in seen:
            continue
        seen.add(heading)
        profile["indications"].append({
            "indication": heading,
            "max_phase": clean_text(ind.get("max_phase_for_ind"), 8),
        })
    profile["indications"] = profile["indications"][:15]

    return profile


def to_record(name: str, rxnorm: dict[str, Any], chembl: dict[str, Any]) -> SourceRecord | None:
    """Fold both pharmacology lookups into one citable compound record."""
    if not rxnorm.get("rxcui") and not chembl.get("chembl_id"):
        return None

    bits: list[str] = []
    display = chembl.get("pref_name") or name.title()

    if chembl:
        bits.append(
            f"{display} is a {(chembl.get('molecule_type') or 'compound').lower()} "
            f"at development stage: {chembl.get('max_phase_label')}."
        )
        if chembl.get("first_approval"):
            bits.append(f"First approval recorded in {chembl['first_approval']}.")
        if chembl.get("black_box_warning"):
            bits.append("ChEMBL flags a black-box warning for this molecule.")
        routes = [r for r, ok in (("oral", chembl.get("oral")), ("parenteral", chembl.get("parenteral"))) if ok]
        if routes:
            bits.append(f"Administered: {', '.join(routes)}.")
        if chembl.get("mechanisms"):
            bits.append("Curated mechanisms: " + "; ".join(
                f"{m['action']} ({m['action_type'].lower()})" if m["action_type"] else m["action"]
                for m in chembl["mechanisms"][:5] if m["action"]
            ) + ".")
        if chembl.get("indications"):
            bits.append("Indications under investigation or approved: " + ", ".join(
                i["indication"] for i in chembl["indications"][:10]
            ) + ".")

    for label, values in (rxnorm.get("classes") or {}).items():
        bits.append(f"{label}: {', '.join(values)}.")

    snippet = clean_text(" ".join(bits), 2600)
    if not snippet:
        return None

    url = (
        f"https://www.ebi.ac.uk/chembl/compound_report_card/{chembl['chembl_id']}/"
        if chembl.get("chembl_id")
        else f"https://mor.nlm.nih.gov/RxNav/search?searchBy=RXCUI&searchTerm={rxnorm['rxcui']}"
    )

    return SourceRecord(
        sid="",
        kind="compound",
        title=f"Pharmacology profile - {display}",
        url=url,
        source="ChEMBL / RxNorm",
        snippet=snippet,
        venue="EMBL-EBI ChEMBL; NLM RxNorm",
        year=int(chembl["first_approval"]) if str(chembl.get("first_approval", "")).isdigit() else None,
        identifiers={
            k: v for k, v in
            {"chembl": chembl.get("chembl_id", ""), "rxcui": rxnorm.get("rxcui", "")}.items() if v
        },
        meta={"rxnorm": rxnorm, "chembl": chembl},
        design="other",
    )
