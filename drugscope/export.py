"""Report export.

Three formats, each for a different destination:

* **Markdown** - the working format. Pastes into Notion, Confluence, a PR, or an
  email, and every citation is a resolvable link.
* **JSON** - the machine format. The full structured report, including every
  appraisal and claim, for a downstream pipeline or a spreadsheet.
* **CSV** - the source register alone, for reference-manager import.

All three carry the same reference list with live URLs, and all three carry the
methodology footer. A report that leaves the app without saying how it was built
and what its limits are is a liability, so the footer is not optional.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from .models import DESIGN_LABELS, TIER_MEANING, Report


def _fmt_citations(sids: list[str]) -> str:
    return " ".join(f"[{s}]" for s in sids) if sids else "_no resolvable citation_"


def to_markdown(report: Report) -> str:
    s = report.synthesis
    m = report.metrics
    lines: list[str] = []

    lines += [
        f"# DrugScope research report",
        "",
        f"**Question.** {report.query}",
        "",
        f"**Interpreted as.** {report.plan.interpretation}",
        "",
        f"_Generated {report.generated_at} &middot; depth: {report.depth} &middot; "
        f"{len(report.records)} sources &middot; {report.elapsed_seconds:.0f}s_",
        "",
        "---",
        "",
        "## Bottom line",
        "",
        f"> {s.summary.verdict}",
        "",
        f"**Confidence: {s.summary.confidence}.** {s.summary.confidence_rationale}",
        "",
        "| Index | Value |",
        "| --- | --- |",
        f"| Evidence Quality Index | {m['quality']['score']}/100 |",
        f"| Consensus Index | {m['consensus']['score']}/100 |",
        f"| Development maturity | {s.summary.maturity}/100 |",
        f"| Sources analysed | {m['totals']['records']} |",
        f"| Structured claims | {m['totals']['claims']} |",
        f"| Evidence span | {m['year_span']} |",
        "",
        "## Executive summary",
        "",
        s.summary.narrative,
        "",
    ]

    if s.summary.watch_items:
        lines += ["### What to watch", ""]
        lines += [f"- {item}" for item in s.summary.watch_items]
        lines += [""]

    lines += ["## Key findings", ""]
    for i, finding in enumerate(s.key_findings, 1):
        lines += [
            f"### {i}. {finding.headline}",
            "",
            f"_Strength: {finding.strength}_ &middot; {_fmt_citations(finding.citations)}",
            "",
            finding.detail,
            "",
            f"**So what.** {finding.so_what}",
            "",
        ]

    if s.comparison.entities and s.comparison.rows:
        lines += ["## Comparison", "", "| Criterion | " + " | ".join(s.comparison.entities) + " | Verdict |"]
        lines += ["| --- " * (len(s.comparison.entities) + 2) + "|"]
        for row in s.comparison.rows:
            by_entity = {cell.entity: cell for cell in row.cells}
            cells = []
            for entity in s.comparison.entities:
                cell = by_entity.get(entity)
                if cell is None:
                    cells.append("Not established")
                else:
                    refs = " ".join(f"[{c}]" for c in cell.citations)
                    cells.append(f"{cell.value.replace('|', '/')} {refs}".strip())
            lines.append(
                f"| **{row.criterion}** | " + " | ".join(cells) + f" | {row.verdict or '-'} |"
            )
        lines += ["", f"**Bottom line.** {s.comparison.bottom_line}", ""]

    if s.timeline:
        lines += ["## Development timeline", "", "| Date | Event | Category | Detail | Sources |", "| --- | --- | --- | --- | --- |"]
        for event in sorted(s.timeline, key=lambda e: e.date or "9999"):
            detail = event.detail.replace("|", "/")
            lines.append(
                f"| {event.date} | {event.label} | {event.category} | {detail} | "
                f"{_fmt_citations(event.citations)} |"
            )
        lines += [""]

    lines += ["## Conflicting evidence", ""]
    if s.conflicts:
        for conflict in s.conflicts:
            lines += [
                f"### {conflict.topic} ({conflict.severity.replace('_', ' ')})",
                "",
                f"**Position A.** {conflict.position_a} {_fmt_citations(conflict.citations_a)}",
                "",
                f"**Position B.** {conflict.position_b} {_fmt_citations(conflict.citations_b)}",
                "",
                f"**Assessment.** {conflict.assessment}",
                "",
                f"**Likely cause of divergence.** {conflict.likely_explanation}",
                "",
            ]
    else:
        lines += [
            "No direct contradictions were identified in the retrieved corpus. "
            "Internal consistency is not the same as correctness - it can also mean "
            "the corpus is too small or too homogeneous to disagree with itself.",
            "",
        ]

    lines += ["## Research gaps and open questions", ""]
    for i, gap in enumerate(s.gaps, 1):
        lines += [
            f"### {i}. {gap.question}",
            "",
            f"_Priority: {gap.priority}_",
            "",
            f"**Why it matters.** {gap.why_it_matters}",
            "",
            f"**Missing from the corpus.** {gap.evidence_absent}",
            "",
            f"**What would answer it.** {gap.what_would_answer_it}",
            "",
        ]

    if s.safety_signals:
        lines += ["## Safety signals", "", "| Event | Seriousness | Frequency | Context | Sources |", "| --- | --- | --- | --- | --- |"]
        for signal in s.safety_signals:
            lines.append(
                f"| {signal.event} | {signal.seriousness} | {signal.frequency or '-'} | "
                f"{signal.context.replace('|', '/')} | {_fmt_citations(signal.citations)} |"
            )
        lines += [""]

    lines += [
        "## Regulatory status",
        "",
        s.regulatory_status,
        "",
        "## Mechanism",
        "",
        s.mechanism,
        "",
    ]

    briefing = m.get("recent_developments", "")
    if briefing and not briefing.startswith("("):
        lines += ["## Recent developments (open web)", "", briefing, ""]

    lines += ["## Sources", ""]
    for record in report.records:
        bits = [f"**[{record.sid}]**", f"[{record.title}]({record.url})"]
        meta = [record.citation, DESIGN_LABELS.get(record.design, record.design), record.tier]
        if record.sample_size:
            meta.append(f"n={record.sample_size:,}")
        for key, value in record.identifiers.items():
            meta.append(f"{key.upper()} {value}")
        lines.append(" ".join(bits) + " - " + " &middot; ".join(m for m in meta if m))
    lines += [""]

    lines += _methodology(report)
    return "\n".join(lines)


def _methodology(report: Report) -> list[str]:
    m = report.metrics
    quality = m["quality"]
    components = "; ".join(
        f"{name} {detail['points']}/{detail['weight']}"
        for name, detail in quality.get("components", {}).items()
    )
    tiers = "; ".join(f"**{tier}** {meaning}" for tier, meaning in TIER_MEANING.items())

    lines = [
        "---",
        "",
        "## Methodology and limitations",
        "",
        "**How this report was produced.** Evidence was retrieved by direct query to "
        f"{', '.join(m['totals']['sources_used'])}. Each record was appraised "
        "individually to extract study design, sample size and discrete claims. "
        "Counts, indices and chart data were computed arithmetically from those "
        "appraisals, not generated by a language model. The narrative was then "
        "synthesised over the structured result, and every citation was verified "
        "against the retrieved corpus before this report was written.",
        "",
        f"**Evidence Quality Index ({quality['score']}/100).** A weighted composite over "
        f"{quality.get('basis', 0)} papers and trials: {components}. It measures the "
        "strength of the retrieved evidence *base*, not the quality of any single study, "
        "and it saturates - the sixth randomised trial adds less than the second.",
        "",
        f"**Consensus Index ({m['consensus']['score']}/100).** For each topic with at least "
        "two directional claims, the share held by the majority direction, weighted by "
        f"claim volume. {m['consensus']['topics_assessed']} topics qualified.",
        "",
        f"**Evidence tiers.** {tiers}.",
        "",
        "**Limitations you should assume are present.**",
        "",
        "- Retrieval is a sample, not a systematic review. Query wording changes what is "
        "found, and a record absent from this corpus is not evidence of absence.",
        "- Appraisal is based on abstracts and registry records, not full texts. "
        "Methodological flaws visible only in a full paper will have been missed.",
        "- Publication bias is not corrected for. Negative and null results are "
        "systematically under-represented in the literature this corpus is drawn from.",
        "- Spontaneous adverse-event report counts are reporting volume, not incidence. "
        "They have no exposure denominator, reporting is voluntary and uneven, and a "
        "report does not establish causation.",
        "- Regulatory coverage is US FDA data only. EMA, MHRA, PMDA and other authorities "
        "are not queried.",
        "- This is research intelligence for qualified professionals. It is not medical "
        "advice, not a treatment recommendation, and not a substitute for a prescriber's "
        "judgement or the approved product label.",
        "",
    ]

    if report.warnings:
        lines += ["**Run diagnostics.**", ""]
        lines += [f"- {w}" for w in report.warnings]
        lines += [""]

    return lines


def to_json(report: Report) -> str:
    payload: dict[str, Any] = {
        "query": report.query,
        "generated_at": report.generated_at,
        "depth": report.depth,
        "elapsed_seconds": report.elapsed_seconds,
        "plan": report.plan.model_dump(),
        "synthesis": report.synthesis.model_dump(),
        "metrics": report.metrics,
        "records": [r.to_dict() for r in report.records],
        "appraisals": {sid: a.model_dump() for sid, a in report.appraisals.items()},
        "usage": {
            "calls": report.usage.calls,
            "input_tokens": report.usage.input_tokens,
            "output_tokens": report.usage.output_tokens,
            "cache_read_tokens": report.usage.cache_read_tokens,
            "estimated_cost_usd": round(report.usage.cost_usd, 4),
        },
        "warnings": report.warnings,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def report_from_json(text: str) -> Report:
    """Rebuild a `Report` from `to_json` output - how saved projects are reopened.

    JSON rather than pickle on purpose: a saved project is data, and loading it must
    never be able to execute anything.
    """
    from dataclasses import fields

    from .models import RecordAppraisal, ResearchPlan, SourceRecord, SynthesisBundle, Usage

    payload = json.loads(text)
    record_fields = {f.name for f in fields(SourceRecord)}
    metrics = payload["metrics"]
    number_check = metrics.get("number_check")
    if number_check and isinstance(number_check.get("findings"), dict):
        # JSON object keys are strings; the UI looks findings up by their integer index.
        number_check["findings"] = {int(k): v for k, v in number_check["findings"].items()}
    usage = payload.get("usage") or {}
    return Report(
        query=payload["query"],
        plan=ResearchPlan.model_validate(payload["plan"]),
        synthesis=SynthesisBundle.model_validate(payload["synthesis"]),
        records=[SourceRecord(**{k: v for k, v in r.items() if k in record_fields}) for r in payload["records"]],
        appraisals={sid: RecordAppraisal.model_validate(a) for sid, a in payload["appraisals"].items()},
        metrics=metrics,
        usage=Usage(
            input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0), calls=usage.get("calls", 0),
            cost_usd=usage.get("estimated_cost_usd", 0.0),
        ),
        elapsed_seconds=payload.get("elapsed_seconds", 0.0),
        depth=payload.get("depth", ""),
        generated_at=payload.get("generated_at", ""),
        warnings=payload.get("warnings") or [],
    )


def to_csv(report: Report) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([
        "handle", "kind", "title", "authors", "venue", "year", "design", "tier",
        "sample_size", "relevance", "source", "url", "identifiers",
    ])
    for record in report.records:
        appraisal = report.appraisals.get(record.sid)
        writer.writerow([
            record.sid,
            record.kind,
            record.title,
            "; ".join(record.authors),
            record.venue,
            record.year or "",
            DESIGN_LABELS.get(record.design, record.design),
            record.tier,
            record.sample_size or "",
            appraisal.relevance if appraisal else "",
            record.source,
            record.url,
            "; ".join(f"{k}={v}" for k, v in record.identifiers.items()),
        ])
    return buffer.getvalue()


def filename_stem(report: Report) -> str:
    safe = "".join(
        ch if ch.isalnum() or ch in " -_" else "" for ch in report.query
    ).strip().replace(" ", "-").lower()[:60]
    return f"drugscope-{safe or 'report'}-{report.generated_at[:10]}"
