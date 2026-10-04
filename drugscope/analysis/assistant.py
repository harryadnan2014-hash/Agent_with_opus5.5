"""The research assistant - questions and answers about one finished report.

It answers from the report and its retrieved sources only, never from general
knowledge, and the citation rule is the same as everywhere else: an answer may only
cite a handle the report's corpus contains, and any other handle is stripped before
the answer is shown.

Each question gets the report's conclusions (verdict, findings, conflicts, gaps,
safety, regulatory status), a one-line index of every source, and the full text of
the sources most relevant to *that* question - ranked lexically, the same way
uploaded documents are. That keeps the prompt small enough for free models while
still putting the actual abstract in front of the model when the question is about
a specific study.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from ..config import SYNTHESIS_MODEL
from ..models import DESIGN_LABELS, Report
from ..providers import make_brain
from ..sources import documents

SYSTEM = """\
You are the research assistant inside DrugScope, a pharmaceutical research \
intelligence tool used by clinicians, researchers and medical-affairs teams.

You answer questions about ONE research report and the sources it retrieved, both \
supplied below. Rules:

1. Answer only from the report and the sources supplied. If they do not cover the \
question, say so plainly ("The retrieved evidence does not address ...") - never fill \
the gap from general knowledge.
2. Cite the handles exactly as given, e.g. [S4] or [T2], after the sentence they \
support. Never invent a handle, a study, a number or an author.
3. Report numbers exactly as the sources state them, and say how strong the evidence \
behind a statement is (randomised trial, observational study, label, etc.).
4. Adverse-event report counts (FAERS) are reporting volume, not incidence - say so \
whenever you mention them.
5. You do not give medical advice. You never tell anyone to start, stop or combine a \
medicine. If asked for personal advice, explain what the evidence says and suggest \
talking to a doctor or pharmacist.
6. Be direct and concise: lead with the answer, then the support. Under about 200 \
words unless the question asks for detail. Plain prose or a short list - no headings."""

_HANDLE_RE = re.compile(r"\[([A-Za-z]{1,2}\d{1,4})\]")


@dataclass
class Answer:
    text: str
    sources_used: list[str]
    removed_citations: int


def _source_line(record: Any, appraisal: Any) -> str:
    bits = [f"[{record.sid}]", record.title[:160], "|", record.source]
    if record.year:
        bits.append(str(record.year))
    bits.append(f"| {DESIGN_LABELS.get(record.design, record.design)}")
    if record.kind == "trial":
        bits.append(f"| {record.meta.get('phase', '')}, {record.meta.get('status', '')}")
    if record.sample_size:
        bits.append(f"| n={record.sample_size:,}")
    if appraisal is not None:
        bits.append(f"| relevance {appraisal.relevance}")
    return " ".join(str(b) for b in bits if b)


def _source_detail(record: Any, appraisal: Any) -> str:
    lines = [_source_line(record, appraisal), f"  Text: {record.snippet[:1600]}"]
    if record.url:
        lines.append(f"  URL: {record.url}")
    if appraisal is not None:
        lines.append(f"  Appraisal: {appraisal.summary}")
        for claim in appraisal.claims[:6]:
            effect = f" ({claim.effect})" if claim.effect.strip() else ""
            lines.append(f"  - [{claim.dimension}/{claim.direction}] {claim.statement}{effect}")
        if appraisal.limitations:
            lines.append(f"  Limitations: {'; '.join(appraisal.limitations[:3])}")
    return "\n".join(lines)


def relevant_sources(report: Report, question: str, limit: int = 8) -> list[str]:
    """Handles of the sources most relevant to a question, best first.

    Explicitly mentioned handles ("what did S4 find?") always come first.
    """
    by_sid = {r.sid: r for r in report.records}
    named = [h.upper() for h in _HANDLE_RE.findall(question) + re.findall(r"\b([STRFCWD]\d{1,4})\b", question)]
    picked = [h for h in dict.fromkeys(named) if h in by_sid]
    chunks = []
    for index, record in enumerate(report.records):
        appraisal = report.appraisals.get(record.sid)
        claims = " ".join(c.statement for c in appraisal.claims) if appraisal else ""
        chunks.append((record.sid, f"{record.title} {record.snippet} {claims}", index))
    for sid, _, _, score in documents.rank_chunks(chunks, [question], limit=limit * 2):
        if score > 0 and sid not in picked:
            picked.append(sid)
    return picked[:limit]


def build_prompt(report: Report, question: str, history: list[dict[str, str]]) -> tuple[str, list[str]]:
    """The user message for one question, and the handles given in full."""
    s = report.synthesis
    m = report.metrics
    parts = [
        f"RESEARCH QUESTION: {report.query}",
        f"INTERPRETED AS: {report.plan.interpretation}",
        f"VERDICT ({s.summary.confidence} confidence): {s.summary.verdict}",
        f"WHY THAT CONFIDENCE: {s.summary.confidence_rationale}",
        f"EXECUTIVE SUMMARY:\n{s.summary.narrative}",
        "KEY FINDINGS:\n" + "\n".join(
            f"{i}. {f.headline} - {f.detail} [{', '.join(f.citations) or 'no citation'}] (strength: {f.strength})"
            for i, f in enumerate(s.key_findings, 1)),
    ]
    if s.comparison.entities and s.comparison.rows:
        rows = []
        for row in s.comparison.rows:
            cells = "; ".join(f"{c.entity}: {c.value} {' '.join(f'[{x}]' for x in c.citations)}" for c in row.cells)
            rows.append(f"- {row.criterion}: {cells}. Verdict: {row.verdict or 'unclear'}")
        parts.append("COMPARISON:\n" + "\n".join(rows) + f"\nBottom line: {s.comparison.bottom_line}")
    if s.conflicts:
        parts.append("CONFLICTING EVIDENCE:\n" + "\n".join(
            f"- {c.topic}: A) {c.position_a} {c.citations_a} vs B) {c.position_b} {c.citations_b}. "
            f"Assessment: {c.assessment}" for c in s.conflicts))
    if s.gaps:
        parts.append("RESEARCH GAPS:\n" + "\n".join(f"- {g.question} ({g.priority})" for g in s.gaps))
    if s.safety_signals:
        parts.append("SAFETY SIGNALS:\n" + "\n".join(
            f"- {x.event} ({x.seriousness}) {x.frequency} - {x.context} {x.citations}" for x in s.safety_signals))
    parts.append(f"REGULATORY STATUS: {s.regulatory_status[:1500]}")
    parts.append(f"MECHANISM: {s.mechanism[:1000]}")
    trials = m.get("trials") or {}
    parts.append(
        f"METRICS: {m['totals']['records']} sources ({m['totals']['papers']} papers, {m['totals']['trials']} trials, "
        f"{m['totals']['regulatory']} regulatory); Evidence Quality Index {m['quality']['score']}/100; "
        f"Consensus Index {m['consensus']['score']}/100; evidence span {m.get('year_span')}; "
        f"highest trial phase {trials.get('highest_phase', 'n/a')}; active trials {trials.get('active', 0)}."
    )
    parts.append("ALL SOURCES (index):\n" + "\n".join(
        _source_line(r, report.appraisals.get(r.sid)) for r in report.records))

    detailed = relevant_sources(report, question)
    by_sid = {r.sid: r for r in report.records}
    if detailed:
        parts.append("MOST RELEVANT SOURCES FOR THIS QUESTION (full text):\n" + "\n\n".join(
            _source_detail(by_sid[h], report.appraisals.get(h)) for h in detailed))
    if history:
        parts.append("CONVERSATION SO FAR:\n" + "\n".join(
            f"{turn['role'].upper()}: {turn['content'][:800]}" for turn in history[-6:]))
    parts.append(f"QUESTION: {question.strip()}")
    return "\n\n".join(parts), detailed


_FULLWIDTH_BRACKETS = re.compile(r"[【［〚]\s*([A-Za-z]{1,2}\d{1,4})\s*[】］〛]")


def normalize_citations(text: str) -> str:
    """Some models cite as 【S4】 or ［S4］; make them [S4] so they are checked and linked."""
    return _FULLWIDTH_BRACKETS.sub(r"[\1]", text or "")


def audit(text: str, valid: set[str]) -> tuple[str, int, list[str]]:
    """Strip citation handles the corpus does not contain. Returns (text, removed, used)."""
    text = normalize_citations(text)
    removed = 0
    used: list[str] = []

    def replace(match: re.Match[str]) -> str:
        nonlocal removed
        handle = match.group(1).upper()
        if handle in valid:
            if handle not in used:
                used.append(handle)
            return f"[{handle}]"
        removed += 1
        return ""

    cleaned = _HANDLE_RE.sub(replace, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+([.,;:!?)])", r"\1", cleaned)
    return cleaned.strip(), removed, used


async def _ask_async(report: Report, question: str, history: list[dict[str, str]], *,
                     provider: str, gateway_model: str) -> Answer:
    brain = make_brain(provider=provider, model=gateway_model, concurrency=2)
    try:
        prompt, _ = build_prompt(report, question, history)
        on_gateway = getattr(brain, "provider", "anthropic") != "anthropic"
        text = await brain.prose(
            model=brain.model if on_gateway else SYNTHESIS_MODEL,
            system=SYSTEM, user=prompt, max_tokens=2500, effort="medium",
        )
    finally:
        await brain.close()
    cleaned, removed, used = audit(text, {r.sid for r in report.records})
    if removed:
        cleaned += ("\n\n_(Removed " + str(removed) + " citation(s) that did not match a retrieved source.)_")
    return Answer(cleaned or "I could not produce an answer from this report.", used, removed)


def ask(report: Report, question: str, history: list[dict[str, str]], *,
        provider: str, gateway_model: str = "") -> Answer:
    """Answer one question about a report. Blocking; raises on provider errors."""
    return asyncio.run(_ask_async(report, question, history, provider=provider, gateway_model=gateway_model))
