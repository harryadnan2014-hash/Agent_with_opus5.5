"""Stage 1 - turn a plain-language question into a retrieval plan.

This stage exists because query quality dominates everything downstream. A search
for `semaglutide cardiovascular` returns a different - and much worse - corpus than
`(semaglutide[tiab] OR Ozempic[tiab]) AND (cardiovascular outcomes[tiab] OR MACE)
AND (randomized controlled trial[pt])`. No amount of clever synthesis recovers from
a bad corpus, so one model call is spent up front getting the queries right.

It also resolves what the user *meant*: whether "compare Ozempic and Mounjaro" is a
treatment comparison (it is), which entities are drugs versus conditions, and which
outcomes the answer has to speak to.
"""

from __future__ import annotations

from ..config import MAX_TOKENS_PLANNING, PLANNING_MODEL
from ..llm import Brain, ThinkingSink
from ..models import ResearchPlan
from .prompts import SYSTEM

PLANNING_GUIDE = """\
TASK: Build the retrieval plan for the research question below.

PubMed query craft - this is the part that decides the quality of everything after \
it:
- Write 2-5 queries, most specific first. The first should be the narrow, \
  high-precision query; the last should be a broader safety net.
- OR together the generic name, brand names and any development code for each drug: \
  `(semaglutide[tiab] OR Ozempic[tiab] OR Wegovy[tiab])`.
- Use `[tiab]` for text words, `[mh]` for MeSH headings, `[pt]` for publication \
  types. To bias toward strong designs, add \
  `AND (randomized controlled trial[pt] OR meta-analysis[pt] OR systematic review[pt])` \
  to at least one query - but never to all of them, or you will miss the safety and \
  real-world literature entirely.
- Keep each query under about 250 characters. Do not use quotation marks around \
  multi-word phrases that already carry a field tag.
- For a disease-landscape question, include one query on current standard of care \
  and one on emerging or investigational therapy.

Trial registry queries are NOT boolean. Give 1-3 short phrases - an intervention \
name, or a condition name - exactly as a registry would list them.

Regulatory terms: generic and brand names only, one per entry, no punctuation. \
Leave the list empty for a question about a disease with no specific drug in scope.

Entities: split what the user gave you into drugs, diseases and targets, and \
populate aliases generously - the alias list is what makes the retrieval find \
records that name the molecule differently.

Comparators: include them when the question is explicitly comparative, and also \
when a reader would obviously want the benchmark (a question about a new GLP-1 \
agonist begs comparison with the established ones). Two to three is the useful \
number; do not pad.

Key outcomes: name the specific endpoints this question turns on, in the vocabulary \
the literature uses. These labels are reused downstream to group claims across \
papers, so make them short, concrete and non-overlapping.

Open questions: what a domain expert would insist on knowing before acting on an \
answer. Not generic caveats - specific to this subject."""


async def build_plan(
    brain: Brain,
    query: str,
    *,
    model: str = PLANNING_MODEL,
    comparators: list[str] | None = None,
    focus: str = "",
    on_thinking: ThinkingSink = None,
) -> ResearchPlan:
    """Produce the retrieval plan for one research question."""
    extras: list[str] = []
    if comparators:
        extras.append(
            "The user explicitly asked to benchmark against: "
            + ", ".join(comparators)
            + ". Include every one of these in `comparators`."
        )
    if focus.strip():
        extras.append(f"The user narrowed the focus to: {focus.strip()}")

    user = "\n\n".join([
        PLANNING_GUIDE,
        f"RESEARCH QUESTION:\n{query.strip()}",
        *extras,
    ])

    return await brain.structured(
        model=model,
        schema_model=ResearchPlan,
        system=SYSTEM,
        user=user,
        max_tokens=MAX_TOKENS_PLANNING,
        effort="high",
        on_thinking=on_thinking,
    )
