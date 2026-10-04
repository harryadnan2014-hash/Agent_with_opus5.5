"""Recent developments: the open-web sweep.

The structured databases have a lag. PubMed indexes a paper weeks after it appears,
ClinicalTrials.gov updates on the sponsor's schedule, and openFDA's approval record
trails the announcement. For a question like "what's new with this drug", that lag is
exactly the part the reader cares about.

So one call runs with the server-side `web_search` tool and sweeps for developments
in the last 18 months. Two design choices keep it from becoming the weak link:

* **Records are built from the search-result blocks, not from the model's prose.** The
  URLs and titles come out of `web_search_tool_result` blocks, which are what the
  search actually returned. A model retyping a URL is a chance to get it wrong.
* **Web records are graded as the weakest evidence in the corpus** and labelled as
  such everywhere they appear. A press release is not a trial result, and the report
  never lets one stand in for the other.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..llm import Brain, LLMError, RefusalError
from ..models import ResearchPlan, SourceRecord
from .prompts import SYSTEM
from ..sources.base import clean_text

log = logging.getLogger("drugscope.websweep")

# Dynamic-filtering variant, supported on the Opus 5 / Sonnet 5 generation.
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 6,
}

# Sources that report pharmaceutical developments rather than sell them. Not a
# quality guarantee, but it keeps supplement marketing and content farms out.
TRUSTED_DOMAINS = [
    "fda.gov", "ema.europa.eu", "who.int", "nih.gov", "nice.org.uk",
    "clinicaltrials.gov", "nejm.org", "thelancet.com", "jamanetwork.com",
    "bmj.com", "nature.com", "science.org", "cell.com", "annals.org",
    "statnews.com", "endpts.com", "fiercebiotech.com", "fiercepharma.com",
    "reuters.com", "biopharmadive.com", "medscape.com", "pharmaceutical-technology.com",
    "europepmc.org", "biorxiv.org", "medrxiv.org", "mhra.gov.uk", "pmda.go.jp",
]

_TASK = """\
TASK: Find what has changed recently on this subject, then report it.

Search the web for developments in the last 18 months. Prioritise, in this order:
1. Regulatory actions - approvals, rejections, label changes, safety communications, \
   withdrawals.
2. Trial readouts - topline results, primary endpoint met or missed, trials halted.
3. Newly published pivotal studies not yet indexed in the databases.
4. Guideline changes and reimbursement or access decisions.

Deliberately ignore: market-size forecasts, share-price commentary, supplement \
marketing, and anything that does not name a specific dated event.

Then write a briefing of 3-6 short paragraphs. For every development, give the date, \
what happened, who announced it, and what it changes. Where a claim comes from a \
company announcement rather than a peer-reviewed result, say so in the sentence - the \
distinction between "Novo reported topline results" and "results were published in \
NEJM" is not a detail.

If you find nothing substantive, say that plainly and briefly. An honest "no material \
developments found in the period" is a useful finding. Do not fill the space."""


def _search_results(message: Any) -> list[dict[str, str]]:
    """Mine `web_search_tool_result` blocks for the URLs the search returned."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", "") != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        # An error result is a single object, not a list - branch before indexing.
        if not isinstance(content, list):
            code = getattr(content, "error_code", None)
            if code:
                log.warning("web search returned error_code=%s", code)
            continue

        for item in content:
            url = clean_text(getattr(item, "url", ""), 400)
            if not url or url in seen:
                continue
            seen.add(url)
            found.append({
                "url": url,
                "title": clean_text(getattr(item, "title", ""), 300) or url,
                "age": clean_text(getattr(item, "page_age", ""), 40),
            })

    return found


async def recent_developments(
    brain: Brain,
    plan: ResearchPlan,
    *,
    model: str,
    query: str,
    effort: str = "medium",
    limit: int = 10,
) -> tuple[str, list[SourceRecord]]:
    """Returns (briefing prose, citable web records). Never raises."""
    entities = [e.name for e in plan.primary_entities] + [e.name for e in plan.comparators]
    today = datetime.now(timezone.utc).strftime("%B %Y")

    user = "\n\n".join([
        _TASK,
        f"TODAY IS: {today}",
        f"SUBJECT: {', '.join(entities) or query}",
        f"RESEARCH QUESTION: {plan.interpretation}",
        f"CONDITIONS IN SCOPE: {', '.join(plan.conditions) or 'unspecified'}",
    ])

    tool = dict(WEB_SEARCH_TOOL)
    tool["allowed_domains"] = TRUSTED_DOMAINS[:64]

    try:
        message = await brain.with_server_tools(
            model=model,
            system=SYSTEM,
            user=user,
            tools=[tool],
            max_tokens=16000,
            effort=effort,
        )
    except RefusalError as exc:
        return f"(web sweep declined: {exc})", []
    except (LLMError, Exception) as exc:  # noqa: BLE001 - one weak stream must not end the run
        log.warning("web sweep failed: %s", exc)
        return "(web sweep unavailable for this run)", []

    if message is None:
        return "(web sweep returned nothing)", []

    briefing = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()

    records: list[SourceRecord] = []
    for hit in _search_results(message)[:limit]:
        records.append(SourceRecord(
            sid="",
            kind="news",
            title=hit["title"],
            url=hit["url"],
            source="Web",
            snippet=(
                f"Retrieved in the recent-developments sweep. {hit['title']}. "
                f"Page age: {hit['age'] or 'unknown'}. Web sources are the weakest "
                "evidence in this corpus and are not peer reviewed unless the "
                "domain indicates a journal."
            ),
            date=hit["age"],
            venue=hit["url"].split("/")[2] if "//" in hit["url"] else "",
            meta={"page_age": hit["age"], "web_sweep": True},
            design="other",
        ))

    return briefing or "(no briefing text returned)", records
