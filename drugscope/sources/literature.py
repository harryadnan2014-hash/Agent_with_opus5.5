"""Scientific literature: Europe PMC and PubMed.

Both indexes are queried because their coverage differs at the edges - Europe PMC
carries preprints and European journals PubMed is slower to pick up, PubMed's MeSH
indexing is better for older work - and because a single index going down should
not empty the literature stream. `dedupe()` in `base` reconciles the overlap on
DOI, then PMID, then normalised title.

Study design is classified **here, deterministically**, from publication-type
metadata plus title/abstract signals. The model later refines it per record, but
the baseline is not model-dependent: evidence grading must not drift between runs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from ..config import NCBI_API_KEY, NCBI_EMAIL, NCBI_TOOL
from ..models import SourceRecord, StudyDesign
from .base import Fetcher, clean_text, first_year

EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Ordered most-specific-first: the first match wins.
_PUBTYPE_MAP: list[tuple[str, StudyDesign]] = [
    ("meta-analysis", "meta_analysis"),
    ("systematic review", "systematic_review"),
    ("practice guideline", "guideline"),
    ("guideline", "guideline"),
    ("randomized controlled trial", "rct"),
    ("controlled clinical trial", "rct"),
    ("clinical trial, phase iv", "rct"),
    ("clinical trial, phase iii", "rct"),
    ("clinical trial, phase ii", "rct"),
    ("clinical trial", "rct"),
    ("observational study", "cohort"),
    ("cohort", "cohort"),
    ("case-control", "case_control"),
    ("cross-sectional", "cross_sectional"),
    ("case reports", "case_report"),
    ("comparative study", "cohort"),
    ("review", "review"),
]

_PRECLINICAL_HINTS = (
    "in vitro", "in-vitro", "cell line", "murine", " mice", " rats", " rat ",
    "zebrafish", "xenograft", "knockout mouse", "molecular docking",
    "animal model", "preclinical",
)
_RCT_HINTS = ("randomised", "randomized", "double-blind", "placebo-controlled", "double blind")
_META_HINTS = ("meta-analysis", "meta analysis", "pooled analysis")


def classify_design(pub_types: list[str], title: str, abstract: str) -> StudyDesign:
    """Deterministic study-design label from metadata, then text signals."""
    joined = " ".join(pub_types).lower()
    for needle, design in _PUBTYPE_MAP:
        if needle in joined:
            return design

    text = f"{title} {abstract}".lower()
    if any(h in text for h in _META_HINTS):
        return "meta_analysis"
    if "systematic review" in text:
        return "systematic_review"
    if any(h in text for h in _RCT_HINTS):
        return "rct"
    if any(h in text for h in _PRECLINICAL_HINTS):
        return "preclinical"
    if "cohort" in text:
        return "cohort"
    if "case report" in text or "we report a case" in text:
        return "case_report"
    return "other"


def _extract_sample_size(text: str) -> int | None:
    """Pull an enrolment number out of an abstract when one is stated plainly."""
    import re

    patterns = [
        r"\b(?:n\s*=\s*)(\d{2,7})\b",
        r"\b(\d{2,7})\s+(?:patients|participants|subjects|adults|children|women|men)\b",
        r"\benrol(?:led|ment of)\s+(\d{2,7})\b",
    ]
    best: int | None = None
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = int(match.group(1))
            if 5 <= value <= 5_000_000 and (best is None or value > best):
                best = value
    return best


# --------------------------------------------------------------------------- #
# Europe PMC
# --------------------------------------------------------------------------- #

async def search_europepmc(
    fetcher: Fetcher,
    query: str,
    *,
    limit: int = 25,
    year_from: int | None = None,
) -> list[SourceRecord]:
    scoped = query
    if year_from:
        scoped = f"({query}) AND (FIRST_PDATE:[{year_from}-01-01 TO 3000-12-31])"

    payload = await fetcher.get(
        EUROPEPMC,
        params={
            "query": scoped,
            "format": "json",
            "pageSize": min(limit, 100),
            "resultType": "core",
            "sort": "CITED desc",
        },
        label="Europe PMC",
    )
    if not payload:
        return []

    hits = (payload.get("resultList") or {}).get("result") or []
    records: list[SourceRecord] = []

    for hit in hits[:limit]:
        title = clean_text(hit.get("title"), 400)
        if not title:
            continue
        abstract = clean_text(hit.get("abstractText"), 3000)
        pub_types = [
            clean_text(t, 80)
            for t in ((hit.get("pubTypeList") or {}).get("pubType") or [])
        ]
        pmid = str(hit.get("pmid") or "")
        pmcid = str(hit.get("pmcid") or "")
        doi = str(hit.get("doi") or "")

        if pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        elif doi:
            url = f"https://doi.org/{doi}"
        elif pmcid:
            url = f"https://europepmc.org/article/PMC/{pmcid}"
        else:
            url = f"https://europepmc.org/search?query={title[:60]}"

        # `resultType=core` nests the journal under journalInfo; the flat
        # `journalTitle` key is only present on some record types.
        journal_info = hit.get("journalInfo") or {}
        venue = clean_text(
            hit.get("journalTitle")
            or ((journal_info.get("journal") or {}).get("title"))
            or ((journal_info.get("journal") or {}).get("medlineAbbreviation"))
            or hit.get("bookOrReportDetails", {}).get("publisher")
            or ("Preprint" if str(hit.get("source") or "") == "PPR" else ""),
            160,
        )
        year = first_year(
            hit.get("pubYear"), journal_info.get("yearOfPublication"), hit.get("firstPublicationDate")
        )
        authors = [
            a.strip()
            for a in clean_text(hit.get("authorString"), 400).split(",")
            if a.strip()
        ][:8]

        records.append(
            SourceRecord(
                sid="",  # minted by the pipeline
                kind="paper",
                title=title,
                url=url,
                source="Europe PMC",
                snippet=abstract or title,
                date=clean_text(hit.get("firstPublicationDate"), 20),
                year=year,
                venue=venue,
                authors=authors,
                identifiers={
                    k: v for k, v in
                    {"pmid": pmid, "pmcid": pmcid, "doi": doi}.items() if v
                },
                meta={
                    "citations": int(hit.get("citedByCount") or 0),
                    "open_access": str(hit.get("isOpenAccess") or "N") == "Y",
                    "pub_types": pub_types,
                },
                design=classify_design(pub_types, title, abstract),
                sample_size=_extract_sample_size(abstract),
            )
        )

    return records


# --------------------------------------------------------------------------- #
# PubMed
# --------------------------------------------------------------------------- #

def _ncbi_params(**extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"tool": NCBI_TOOL, **extra}
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    if NCBI_EMAIL:
        params["email"] = NCBI_EMAIL
    return params


def _node_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return clean_text("".join(node.itertext()), 4000)


def _parse_pubmed_article(article: ET.Element) -> SourceRecord | None:
    citation = article.find("MedlineCitation")
    if citation is None:
        return None

    pmid = _node_text(citation.find("PMID"))
    art = citation.find("Article")
    if art is None or not pmid:
        return None

    title = _node_text(art.find("ArticleTitle"))
    if not title:
        return None

    # Abstracts are split into labelled sections (BACKGROUND / METHODS / ...).
    parts: list[str] = []
    for chunk in art.findall("./Abstract/AbstractText"):
        label = chunk.get("Label")
        body = _node_text(chunk)
        parts.append(f"{label}: {body}" if label else body)
    abstract = clean_text(" ".join(parts), 3000)

    pub_types = [_node_text(pt) for pt in art.findall("./PublicationTypeList/PublicationType")]

    journal = _node_text(art.find("./Journal/Title"))
    year = first_year(
        _node_text(art.find("./Journal/JournalIssue/PubDate/Year")),
        _node_text(art.find("./Journal/JournalIssue/PubDate/MedlineDate")),
        _node_text(citation.find("./DateRevised/Year")),
    )

    authors: list[str] = []
    for author in art.findall("./AuthorList/Author")[:8]:
        last = _node_text(author.find("LastName"))
        initials = _node_text(author.find("Initials"))
        collective = _node_text(author.find("CollectiveName"))
        if last:
            authors.append(f"{last} {initials}".strip())
        elif collective:
            authors.append(collective)

    doi = ""
    for ident in article.findall(".//ArticleIdList/ArticleId"):
        if ident.get("IdType") == "doi":
            doi = _node_text(ident)
            break

    mesh = [_node_text(m) for m in citation.findall("./MeshHeadingList/MeshHeading/DescriptorName")][:12]

    return SourceRecord(
        sid="",
        kind="paper",
        title=title,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        source="PubMed",
        snippet=abstract or title,
        date=str(year or ""),
        year=year,
        venue=journal,
        authors=authors,
        identifiers={k: v for k, v in {"pmid": pmid, "doi": doi}.items() if v},
        meta={"pub_types": pub_types, "mesh": mesh},
        design=classify_design(pub_types, title, abstract),
        sample_size=_extract_sample_size(abstract),
    )


async def search_pubmed(
    fetcher: Fetcher,
    query: str,
    *,
    limit: int = 25,
    year_from: int | None = None,
) -> list[SourceRecord]:
    term = query
    if year_from:
        term = f"({query}) AND ({year_from}:3000[dp])"

    found = await fetcher.get(
        f"{EUTILS}/esearch.fcgi",
        params=_ncbi_params(
            db="pubmed", term=term, retmax=min(limit, 100),
            retmode="json", sort="relevance",
        ),
        label="PubMed search",
    )
    if not found:
        return []

    ids = ((found.get("esearchresult") or {}).get("idlist")) or []
    if not ids:
        return []

    xml = await fetcher.get(
        f"{EUTILS}/efetch.fcgi",
        params=_ncbi_params(db="pubmed", id=",".join(ids[:limit]), retmode="xml"),
        as_json=False,
        label="PubMed fetch",
    )
    if not xml:
        return []

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        fetcher.note("PubMed returned malformed XML; that batch was skipped")
        return []

    records: list[SourceRecord] = []
    for article in root.findall(".//PubmedArticle"):
        parsed = _parse_pubmed_article(article)
        if parsed is not None:
            records.append(parsed)
    return records
