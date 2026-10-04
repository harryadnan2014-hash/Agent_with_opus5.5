"""Official reference data for the community features.

Four public, keyless services, each answering one question:

* **RxNorm (RxNav)** - what is this medication? Brand names resolve to their active
  ingredient ("Ozempic" -> semaglutide), so reports about the same drug pool together.
* **openFDA drug labels** - what does the approved label say about this food? Label
  sections are scanned for each food's keywords and the matching sentences quoted.
* **openFDA FAERS** - how many spontaneous reports name this drug and reaction?
  Reporting volume only, never incidence.
* **DailyMed** - links to the current label documents.

Rules that keep official data honest:

* Results are stored in `evidence_sources` with their source URL, retrieval time and
  provenance `official_source`, apart from anything a user submitted, and they never
  overwrite community data.
* Every function returns a result with a `status` - `ok`, `not_found` or
  `unavailable` - instead of raising, so the app stays usable when a service is down,
  slow or rate-limiting.
* Coverage is US labelling. UAE and other markets can differ, and the UI says so.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx

from ..sources.base import clean_text
from .db import connect, now_iso

log = logging.getLogger("drugscope.community.sources")

RXNAV = "https://rxnav.nlm.nih.gov/REST"
OPENFDA = "https://api.fda.gov/drug"
DAILYMED = "https://dailymed.nlm.nih.gov/dailymed/services/v2"

TIMEOUT = httpx.Timeout(12.0, connect=6.0)
CACHE_DAYS = 7

# Label sections worth reading for food guidance, in reading order.
LABEL_SECTIONS = [
    "boxed_warning", "drug_interactions", "warnings_and_cautions", "warnings", "precautions",
    "dosage_and_administration", "information_for_patients", "patient_medication_information",
    "spl_patient_package_insert", "clinical_pharmacology", "pharmacokinetics",
]
# ...and for drug-drug interactions.
INTERACTION_SECTIONS = [
    "boxed_warning", "contraindications", "drug_interactions", "drug_interactions_table",
    "warnings_and_cautions", "warnings", "precautions",
]
_ALL_SECTIONS = list(dict.fromkeys(LABEL_SECTIONS + INTERACTION_SECTIONS))
SECTION_LABELS = {s: s.replace("_table", "").replace("_", " ").replace("spl ", "").capitalize()
                  for s in _ALL_SECTIONS}

# Excipients and chemicals whose names contain a food keyword.
_FALSE_FRIENDS = re.compile(
    r"\b(benzyl|cetyl|stearyl|cetostearyl|polyvinyl|isopropyl|lanolin|lauryl|phenethyl)\s+alcohol",
    re.IGNORECASE,
)


@dataclass
class Result:
    status: str  # "ok" | "not_found" | "unavailable"
    data: Any = None
    source: str = ""
    url: str = ""
    retrieved_at: str = ""
    message: str = ""
    cached: bool = False


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, follow_redirects=True,
                        headers={"Accept": "application/json", "User-Agent": "DrugScope/1.0"})


def _get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> tuple[int, Any]:
    response = client.get(url, params=params)
    if response.status_code == 404:
        return 404, None
    if response.status_code == 429:
        raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)
    response.raise_for_status()
    return response.status_code, response.json()


def _unavailable(source: str, exc: Exception) -> Result:
    log.warning("%s unavailable: %s", source, exc)
    reason = "rate limited - try again in a minute" if "429" in str(exc) or "rate" in str(exc).lower() \
        else "did not respond"
    return Result("unavailable", source=source, message=f"{source} {reason}. The rest of the page still works.")


# --------------------------------------------------------------------------- #
# Cache in evidence_sources
# --------------------------------------------------------------------------- #

def _cached(kind: str, key: str) -> Result | None:
    since = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM evidence_sources WHERE kind = ? AND lookup_key = ? AND retrieved_at >= ? "
            "ORDER BY retrieved_at DESC LIMIT 1",
            (kind, key, since),
        ).fetchone()
    if row is None:
        return None
    meta = json.loads(row["metadata"] or "{}")
    return Result(meta.get("status", "ok"), meta.get("data"), row["source"], row["url"],
                  row["retrieved_at"], meta.get("message", ""), cached=True)


def _store(kind: str, key: str, result: Result, *, medication: str = "", food: str = "",
           symptom: str = "", title: str = "", excerpt: str = "") -> Result:
    """Keep an official lookup, with its provenance, apart from community data."""
    if result.status == "unavailable":
        return result  # never cache an outage
    result.retrieved_at = now_iso()
    with connect() as conn:
        conn.execute(
            "INSERT INTO evidence_sources (provenance, source, kind, lookup_key, medication, food, "
            "symptom, title, url, excerpt, metadata, retrieved_at) "
            "VALUES ('official_source', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (result.source, kind, key, medication, food, symptom, title[:300], result.url or "",
             excerpt[:2000], json.dumps({"status": result.status, "data": result.data,
                                         "message": result.message}), result.retrieved_at),
        )
    return result


# --------------------------------------------------------------------------- #
# RxNorm - medication normalisation
# --------------------------------------------------------------------------- #

def normalize_medication(term: str) -> Result:
    """Candidate medications for what someone typed, as active ingredients.

    data: list of {"ingredient", "brands", "rxcui"}.
    """
    term = clean_text(term, 80)
    if len(term) < 2:
        return Result("not_found", [], "RxNorm")
    try:
        with _client() as client:
            _, payload = _get_json(client, f"{RXNAV}/approximateTerm.json",
                                   {"term": term, "maxEntries": 8})
            candidates = ((payload or {}).get("approximateGroup") or {}).get("candidate") or []
            seen: dict[str, dict[str, Any]] = {}
            for candidate in candidates:
                rxcui = candidate.get("rxcui")
                if not rxcui or len(seen) >= 4:
                    continue
                _, related = _get_json(client, f"{RXNAV}/rxcui/{rxcui}/related.json", {"tty": "IN BN"})
                groups = ((related or {}).get("relatedGroup") or {}).get("conceptGroup") or []
                names = {g.get("tty"): [p["name"] for p in g.get("conceptProperties") or []] for g in groups}
                for ingredient in names.get("IN", [])[:1]:
                    entry = seen.setdefault(ingredient.lower(), {
                        "ingredient": ingredient, "brands": [], "rxcui": rxcui,
                    })
                    for brand in names.get("BN", [])[:4]:
                        if brand not in entry["brands"]:
                            entry["brands"].append(brand)
    except (httpx.HTTPError, ValueError) as exc:
        return _unavailable("RxNorm", exc)
    data = list(seen.values())
    return Result("ok" if data else "not_found", data, "RxNorm",
                  f"https://mor.nlm.nih.gov/RxNav/search?searchBy=String&searchTerm={quote(term)}")


# --------------------------------------------------------------------------- #
# openFDA labels - what the label says about food
# --------------------------------------------------------------------------- #

def _keyword_pattern(keywords: str) -> re.Pattern[str]:
    words = [re.escape(k.strip()) for k in keywords.split("|") if k.strip()]
    return re.compile(r"\b(" + "|".join(words) + r")", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", text) if len(s.strip()) > 20]


def scan_label(label: dict[str, Any], foods: list[dict[str, Any]], per_food: int = 4) -> dict[str, list[dict[str, str]]]:
    """Sentences in a label that mention each food, with the section they came from."""
    found: dict[str, list[dict[str, str]]] = {}
    patterns = {f["name"]: _keyword_pattern(f["keywords"]) for f in foods if f.get("keywords")}
    for section in LABEL_SECTIONS:
        text = clean_text(label.get(section), 30000)
        if not text:
            continue
        for sentence in _sentences(text):
            for food, pattern in patterns.items():
                if len(found.get(food, [])) >= per_food or not pattern.search(sentence):
                    continue
                if food.lower().startswith("alcohol") and not pattern.search(_FALSE_FRIENDS.sub("", sentence)):
                    continue
                quote_text = sentence if len(sentence) <= 420 else sentence[:417] + "..."
                bucket = found.setdefault(food, [])
                if all(q["text"] != quote_text for q in bucket):
                    bucket.append({"section": SECTION_LABELS.get(section, section), "text": quote_text})
    return found


def label_text(ingredient: str) -> Result:
    """The US label's relevant sections as plain text, cached.

    data: {"title", "set_id", "effective", "sections": {section: text}}. Callers scan
    the cached text, so a new food or a different second drug never needs a refetch.
    """
    key = ingredient.strip().lower()
    cached = _cached("label_text", key)
    if cached is not None:
        return cached
    safe = ingredient.replace('"', "").strip()
    query = (f'openfda.generic_name:"{safe}" OR openfda.substance_name:"{safe}" '
             f'OR openfda.brand_name:"{safe}"')
    try:
        with _client() as client:
            status, payload = _get_json(client, f"{OPENFDA}/label.json", {"search": query, "limit": 1})
    except (httpx.HTTPError, ValueError) as exc:
        return _unavailable("openFDA drug labels", exc)
    if status == 404 or not (payload or {}).get("results"):
        result = Result("not_found", {"sections": {}}, "openFDA drug labels",
                        message=f"No US label found for {ingredient}.")
        return _store("label_text", key, result, medication=ingredient)

    label = payload["results"][0]
    openfda = label.get("openfda") or {}
    set_id = clean_text(label.get("set_id"), 60)
    title = clean_text((openfda.get("brand_name") or [ingredient])[0], 80)
    url = (f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}" if set_id
           else "https://open.fda.gov/apis/drug/label/")
    sections = {s: clean_text(label.get(s), 30000) for s in _ALL_SECTIONS if label.get(s)}
    result = Result("ok", {"title": title, "set_id": set_id,
                           "effective": clean_text(label.get("effective_time"), 12),
                           "sections": sections}, "openFDA drug labels", url)
    return _store("label_text", key, result, medication=ingredient, title=f"Label - {title}",
                  excerpt=(sections.get("drug_interactions") or "")[:600])


def label_food_mentions(ingredient: str, foods: list[dict[str, Any]]) -> Result:
    """data: {"title", "set_id", "mentions": {food: [{"section", "text"}]}}"""
    label = label_text(ingredient)
    if label.status != "ok":
        label.data = {"mentions": {}}
        return label
    data = dict(label.data)
    data["mentions"] = scan_label(data.pop("sections"), foods)
    return Result("ok", data, label.source, label.url, label.retrieved_at, cached=label.cached)


# --------------------------------------------------------------------------- #
# Drug <-> drug
# --------------------------------------------------------------------------- #

# Plain-language and abbreviated names labels use for a pharmacologic class.
_CLASS_SYNONYMS = {
    "nonsteroidal anti-inflammatory drug": ["nsaid", "nonsteroidal anti-inflammatory"],
    "proton pump inhibitor": ["ppi"],
    "serotonin reuptake inhibitor": ["ssri", "selective serotonin reuptake inhibitor", "serotonergic drug"],
    "serotonin and norepinephrine reuptake inhibitor": ["snri", "serotonergic drug"],
    "monoamine oxidase inhibitor": ["maoi", "mao inhibitor"],
    "hmg-coa reductase inhibitor": ["statin"],
    "vitamin k antagonist": ["anticoagulant", "coumarin"],
    "factor xa inhibitor": ["anticoagulant"],
    "direct thrombin inhibitor": ["anticoagulant"],
    "p2y12 platelet inhibitor": ["antiplatelet"],
    "platelet aggregation inhibitor": ["antiplatelet"],
    "angiotensin converting enzyme inhibitor": ["ace inhibitor"],
    "angiotensin 2 receptor blocker": ["angiotensin receptor blocker", "arb"],
    "beta adrenergic blocker": ["beta blocker", "beta-blocker", "beta-adrenergic blocking agent"],
    "opioid agonist": ["opioid"],
    "macrolide antimicrobial": ["macrolide"],
    "thiazide diuretic": ["thiazide"],
}


def class_phrases(class_name: str) -> list[str]:
    """How a label might name this class: the name itself, CYP shorthand, synonyms."""
    name = re.sub(r"\s*\[.*?\]", "", class_name).strip().lower()
    singular = re.sub(r"s$", "", name)
    phrases = {singular}
    cyp = re.match(r"cytochrome p450 (\w+) (inhibitor|inducer)", singular)
    if cyp:
        phrases |= {f"cyp{cyp.group(1)} {cyp.group(2)}", f"{cyp.group(2)}s? of cyp{cyp.group(1)}"}
    if "p-glycoprotein" in singular:
        kind = "inhibitor" if "inhibitor" in singular else "inducer"
        phrases |= {f"p-gp {kind}", f"pgp {kind}", f"p-glycoprotein {kind}"}
    phrases |= set(_CLASS_SYNONYMS.get(singular, []))
    return sorted(phrases)


def drug_classes(ingredient: str) -> Result:
    """FDA-label pharmacologic classes (EPC) and mechanisms (MoA) from RxClass.

    Only the FDA structured-label source is used: the wider RxClass graph mixes in
    classes from combination products (sertraline appears as an "MAO inhibitor"
    there), which would produce false class-level matches.
    data: list of {"name", "type"}.
    """
    key = ingredient.lower()
    cached = _cached("drug_classes", key)
    if cached is not None:
        return cached
    try:
        with _client() as client:
            _, payload = _get_json(client, f"{RXNAV}/rxclass/class/byDrugName.json",
                                   {"drugName": ingredient, "relaSource": "FDASPL"})
    except (httpx.HTTPError, ValueError) as exc:
        return _unavailable("RxClass", exc)
    seen: dict[str, dict[str, str]] = {}
    for item in ((payload or {}).get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo") or []:
        concept = item.get("rxclassMinConceptItem") or {}
        if concept.get("classType") in ("EPC", "MOA") and concept.get("className"):
            seen.setdefault(concept["className"], {"name": concept["className"], "type": concept["classType"]})
    rows = list(seen.values())
    result = Result("ok" if rows else "not_found", rows, "RxClass (FDA labels)",
                    f"https://mor.nlm.nih.gov/RxClass/search?query={quote(ingredient)}&searchBy=DRUG")
    return _store("drug_classes", key, result, medication=ingredient,
                  title=f"Drug classes - {ingredient}", excerpt=", ".join(r["name"] for r in rows))


def _scan_for_drug(sections: dict[str, str], names: list[str], classes: list[dict[str, str]],
                   per_kind: int = 5) -> dict[str, list[dict[str, str]]]:
    """Sentences naming the other drug directly, or a class it belongs to."""
    direct_words = [re.escape(n.lower()) for n in names if len(n) >= 4]
    direct = re.compile(r"\b(" + "|".join(direct_words) + r")\b", re.I) if direct_words else None
    class_patterns = [
        (c["name"], re.compile(r"\b(" + "|".join(p.replace(" ", r"[\s-]+") for p in class_phrases(c["name"]))
                               + r")(e?s)?\b", re.I))
        for c in classes
    ]
    found: dict[str, list[dict[str, str]]] = {"direct": [], "class": []}
    for section in INTERACTION_SECTIONS:
        text = sections.get(section) or ""
        for sentence in _sentences(text):
            quote_text = sentence if len(sentence) <= 480 else sentence[:477] + "..."
            label = SECTION_LABELS.get(section, section)
            if direct and direct.search(sentence):
                if len(found["direct"]) < per_kind and all(q["text"] != quote_text for q in found["direct"]):
                    found["direct"].append({"section": label, "text": quote_text})
                continue
            for class_name, pattern in class_patterns:
                if pattern.search(sentence) and len(found["class"]) < per_kind \
                        and all(q["text"] != quote_text for q in found["class"]):
                    found["class"].append({"section": label, "text": quote_text, "class": class_name})
                    break
    return found


def label_drug_mentions(first: dict[str, Any], second: dict[str, Any]) -> Result:
    """What each drug's US label says about the other - both directions.

    `first`/`second`: {"ingredient", "brands"}. data: {"first", "second"} each with
    "title", "url", "status", "direct", "class" and "classes" (the other drug's
    classes that were searched for).
    """
    sides: dict[str, Any] = {}
    unavailable = []
    for key, this, other in (("first", first, second), ("second", second, first)):
        label = label_text(this["ingredient"])
        classes = drug_classes(other["ingredient"])
        if label.status == "unavailable":
            unavailable.append(label.message)
        if classes.status == "unavailable":
            unavailable.append(classes.message)
        side = {"title": this["ingredient"], "url": label.url, "status": label.status,
                "direct": [], "class": [], "classes": [c["name"] for c in (classes.data or [])]}
        if label.status == "ok":
            side["title"] = label.data.get("title") or this["ingredient"]
            names = [other["ingredient"], *(other.get("brands") or [])]
            side.update(_scan_for_drug(label.data["sections"], names, classes.data or []))
        sides[key] = side
    if unavailable and all(s["status"] != "ok" for s in sides.values()):
        return Result("unavailable", sides, "openFDA drug labels", message=unavailable[0])
    found = any(s["direct"] or s["class"] for s in sides.values())
    return Result("ok" if found else "not_found", sides, "openFDA drug labels + RxClass",
                  message="; ".join(dict.fromkeys(unavailable)))


def faers_pair(first: str, second: str, top: int = 8) -> Result:
    """Spontaneous reports listing both drugs, with the reactions most often named.

    data: {"total", "reactions": [{"term", "count"}]}. Reporting volume only.
    """
    a, b = sorted([first.lower(), second.lower()])
    key = f"{a}|{b}"
    cached = _cached("faers_pair_drugs", key)
    if cached is not None:
        return cached

    def drug(name: str) -> str:
        safe = name.replace('"', "")
        return f'(patient.drug.openfda.generic_name:"{safe}" OR patient.drug.medicinalproduct:"{safe}")'

    search = f"{drug(a)} AND {drug(b)}"
    try:
        with _client() as client:
            status, total_payload = _get_json(client, f"{OPENFDA}/event.json", {"search": search, "limit": 1})
            reactions_payload = None
            if status != 404:
                _, reactions_payload = _get_json(client, f"{OPENFDA}/event.json", {
                    "search": search, "count": "patient.reaction.reactionmeddrapt.exact", "limit": top})
    except (httpx.HTTPError, ValueError) as exc:
        return _unavailable("openFDA FAERS", exc)
    total = int((((total_payload or {}).get("meta") or {}).get("results") or {}).get("total") or 0)
    reactions = [{"term": clean_text(r.get("term"), 80).title(), "count": int(r.get("count") or 0)}
                 for r in ((reactions_payload or {}).get("results") or [])[:top]]
    result = Result("ok" if total else "not_found", {"total": total, "reactions": reactions},
                    "openFDA FAERS", "https://open.fda.gov/apis/drug/event/")
    return _store("faers_pair_drugs", key, result, medication=f"{a} + {b}",
                  title=f"FAERS reports listing {a} and {b}", excerpt=f"{total} reports")


def drugs_mentioning_food(food: dict[str, Any], limit: int = 15) -> Result:
    """Medications whose US labels mention this food in interaction or patient guidance.

    data: list of {"generic_name", "label_documents"} - the count is how many label
    documents (one per manufacturer or packager) mention it, not a measure of risk.
    """
    key = food["name"].lower()
    cached = _cached("food_drugs", key)
    if cached is not None:
        return cached
    keywords = [k.strip() for k in food.get("keywords", "").split("|") if k.strip()][:4]
    if not keywords:
        return Result("not_found", [], "openFDA drug labels")
    terms = " OR ".join(f'"{k}"' for k in keywords)
    search = f"(drug_interactions:({terms}) OR information_for_patients:({terms}))"
    try:
        with _client() as client:
            status, payload = _get_json(client, f"{OPENFDA}/label.json", {
                "search": search, "count": "openfda.generic_name.exact", "limit": limit,
            })
    except (httpx.HTTPError, ValueError) as exc:
        return _unavailable("openFDA drug labels", exc)
    rows = [{"generic_name": r["term"].title(), "label_documents": int(r["count"])}
            for r in ((payload or {}).get("results") or [])]
    result = Result("ok" if rows else "not_found", rows, "openFDA drug labels",
                    "https://open.fda.gov/apis/drug/label/")
    return _store("food_drugs", key, result, food=food["name"],
                  title=f"US labels mentioning {food['name']}",
                  excerpt=", ".join(r["generic_name"] for r in rows[:10]))


# --------------------------------------------------------------------------- #
# FAERS and DailyMed
# --------------------------------------------------------------------------- #

def faers_count(ingredient: str, meddra_term: str) -> Result:
    """Spontaneous reports naming this drug and reaction. data: int."""
    if not meddra_term:
        return Result("not_found", None, "openFDA FAERS")
    key = f"{ingredient.lower()}|{meddra_term.lower()}"
    cached = _cached("faers_pair", key)
    if cached is not None:
        return cached
    safe = ingredient.replace('"', "")
    search = (f'(patient.drug.openfda.generic_name:"{safe}" OR patient.drug.medicinalproduct:"{safe}") '
              f'AND patient.reaction.reactionmeddrapt:"{meddra_term}"')
    try:
        with _client() as client:
            status, payload = _get_json(client, f"{OPENFDA}/event.json", {"search": search, "limit": 1})
    except (httpx.HTTPError, ValueError) as exc:
        return _unavailable("openFDA FAERS", exc)
    total = int((((payload or {}).get("meta") or {}).get("results") or {}).get("total") or 0)
    result = Result("ok" if total else "not_found", total, "openFDA FAERS",
                    "https://open.fda.gov/apis/drug/event/")
    return _store("faers_pair", key, result, medication=ingredient, symptom=meddra_term,
                  title=f"FAERS reports: {ingredient} + {meddra_term}", excerpt=f"{total} reports")


def dailymed_labels(ingredient: str, limit: int = 5) -> Result:
    """Current label documents. data: list of {"title", "published", "url"}."""
    key = ingredient.lower()
    cached = _cached("dailymed", key)
    if cached is not None:
        return cached
    try:
        with _client() as client:
            status, payload = _get_json(client, f"{DAILYMED}/spls.json",
                                        {"drug_name": ingredient, "pagesize": limit})
    except (httpx.HTTPError, ValueError) as exc:
        return _unavailable("DailyMed", exc)
    rows = [{
        "title": clean_text(d.get("title"), 160),
        "published": clean_text(d.get("published_date"), 20),
        "url": f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={d.get('setid')}",
    } for d in ((payload or {}).get("data") or [])[:limit] if d.get("setid")]
    result = Result("ok" if rows else "not_found", rows, "DailyMed",
                    f"https://dailymed.nlm.nih.gov/dailymed/search.cfm?query={quote(ingredient)}")
    return _store("dailymed", key, result, medication=ingredient,
                  title=f"DailyMed labels - {ingredient}",
                  excerpt="; ".join(r["title"] for r in rows[:3]))
