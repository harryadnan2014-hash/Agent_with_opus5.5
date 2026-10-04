"""Official-source helpers: label scanning, caching, and outage handling - offline."""

import httpx

from drugscope.community import service, sources


def test_scan_label_finds_food_sentences_and_ignores_excipients():
    label = {
        "drug_interactions": [
            "Grapefruit juice can raise plasma concentrations. Avoid large quantities of grapefruit juice."
        ],
        "warnings": ["This injection contains benzyl alcohol, which has been linked to toxicity in neonates."],
        "information_for_patients": ["Avoid drinking alcohol while taking this medicine."],
    }
    found = sources.scan_label(label, service.foods())
    assert "Grapefruit" in found and found["Grapefruit"][0]["section"] == "Drug interactions"
    alcohol = [q["text"] for q in found["Alcohol"]]
    assert alcohol == ["Avoid drinking alcohol while taking this medicine."]  # benzyl alcohol skipped


def test_outage_returns_unavailable_and_is_not_cached(monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.Client, "get", boom)
    for result in (
        sources.normalize_medication("lipitor"),
        sources.label_food_mentions("atorvastatin", service.foods()),
        sources.drugs_mentioning_food(service.foods()[0]),
        sources.faers_count("atorvastatin", "myalgia"),
        sources.dailymed_labels("atorvastatin"),
    ):
        assert result.status == "unavailable" and "still works" in result.message
    assert sources._cached("label_food", "atorvastatin") is None  # an outage is never cached


def test_rate_limit_is_reported_as_such(monkeypatch):
    def limited(self, url, params=None):
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", limited)
    result = sources.faers_count("atorvastatin", "myalgia")
    assert result.status == "unavailable" and "rate limited" in result.message


def test_successful_lookup_is_cached_with_provenance(monkeypatch):
    calls = {"n": 0}

    def fake(self, url, params=None):
        calls["n"] += 1
        return httpx.Response(200, json={"meta": {"results": {"total": 42}}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake)
    first = sources.faers_count("atorvastatin", "myalgia")
    second = sources.faers_count("atorvastatin", "myalgia")
    assert (first.data, second.data, second.cached, calls["n"]) == (42, 42, True, 1)
    from drugscope.community.db import connect
    with connect() as conn:
        row = conn.execute("SELECT provenance, source, url FROM evidence_sources").fetchone()
    assert row["provenance"] == "official_source" and row["source"] == "openFDA FAERS" and row["url"]


def test_fetcher_survives_a_new_event_loop_per_run():
    """Streamlit gives every research run its own event loop. A lock shared across
    runs used to fail every contended request after the first run."""
    import asyncio

    from drugscope.sources.base import Fetcher

    async def contended():
        async with Fetcher() as fetcher:
            await asyncio.gather(*(fetcher._throttle("example.org") for _ in range(3)))

    asyncio.run(contended())
    asyncio.run(contended())  # raised RuntimeError("... bound to a different event loop") before


def test_number_check_flags_only_unquoted_figures():
    from drugscope.analysis.synthesize import check_numbers
    from drugscope.models import (ComparisonMatrix, ExecutiveSummary, KeyFinding, SourceRecord,
                                  SynthesisBundle)

    record = SourceRecord(sid="S1", kind="paper", title="Trial", url="u", source="PubMed",
                          snippet="MACE occurred in 6.5% vs 8.0% (HR 0.80) among 17,604 patients.")
    finding = KeyFinding(headline="MACE fell from 8.0% to 6.5%",
                         detail="HR 0.80 in 17,604 patients, about a 25% relative reduction over 3 trials.",
                         so_what="", strength="strong", citations=["S1"])
    bundle = SynthesisBundle(
        summary=ExecutiveSummary(verdict="", narrative="", confidence="high", confidence_rationale="",
                                 consensus=0, maturity=0, watch_items=[]),
        key_findings=[finding], comparison=ComparisonMatrix(entities=[], rows=[], bottom_line=""),
        timeline=[], conflicts=[], gaps=[], safety_signals=[], regulatory_status="", mechanism="")
    result = check_numbers(bundle, {"S1": record}, {})
    assert result["findings"][1]["unverified"] == ["25"]  # derived; "3 trials" is not checked
    assert result["checked"] == 5


def test_class_phrases_cover_label_wording():
    import re

    phrases = sources.class_phrases("Cytochrome P450 3A4 Inhibitors")
    pattern = re.compile("|".join(p.replace(" ", r"[\s-]+") for p in phrases), re.I)
    assert pattern.search("Avoid strong CYP3A4 inhibitors such as clarithromycin")
    assert pattern.search("inhibitors of CYP3A4")
    assert "nsaid" in sources.class_phrases("Nonsteroidal Anti-inflammatory Drug")


def test_presentation_and_document_build_from_a_report():
    import io

    from docx import Document
    from pptx import Presentation

    from drugscope import office
    from tests.test_ui import _small_report

    report = _small_report("semaglutide and the heart")
    deck = Presentation(io.BytesIO(office.to_pptx(report)))
    text = " ".join(sh.text_frame.text for sl in deck.slides for sh in sl.shapes if sh.has_text_frame)
    assert len(deck.slides) >= 6 and "Semaglutide cut MACE" in text and "[S1]" in text
    doc = Document(io.BytesIO(office.to_docx(report)))
    body = " ".join(p.text for p in doc.paragraphs)
    assert "Bottom line" in body and "SELECT trial of semaglutide" in body and "not medical advice" in body
