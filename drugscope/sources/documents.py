"""User-supplied documents - the retrieval-augmented half of the corpus.

Upload a protocol, an internal memo, a competitor's dossier or a paper that is not
indexed anywhere, and it joins the same corpus as everything the public databases
returned: chunked, ranked against the research question, appraised record by record,
and citable by handle like any other source.

**Why there is no vector database here.** Retrieval quality over a handful of
documents is dominated by chunking and ranking, not by embedding sophistication, and
a local index would add a dependency, a build step and a cache to invalidate for no
measurable gain at this scale. Chunks are scored with BM25-style lexical relevance
against the planned query terms, which is transparent, instant, has no cold start,
and - unlike an embedding score - can be explained to a reviewer. If document volume
ever grows past a few dozen files, this is the module to swap.

Uploaded chunks are graded `Tier 3` by default: a document a user supplies has no
peer review behind it, and the report should never imply otherwise.
"""

from __future__ import annotations

import io
import logging
import math
import re
from collections import Counter
from typing import Any, Iterable

from ..models import SourceRecord
from .base import clean_text

log = logging.getLogger("drugscope.documents")

SUPPORTED = ("pdf", "txt", "md", "markdown", "csv", "tsv", "json")

# Roughly 400 words per chunk with 60 words of overlap, so a claim that straddles a
# boundary still appears whole in one of them.
CHUNK_WORDS = 400
CHUNK_OVERLAP = 60

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "was", "were", "are", "have",
    "has", "had", "not", "but", "any", "all", "can", "its", "than", "then", "they",
    "their", "which", "who", "what", "when", "how", "into", "onto", "over", "under",
    "between", "among", "also", "such", "been", "being", "does", "did", "will", "would",
    "may", "might", "should", "could", "each", "other", "more", "most", "some", "only",
    "our", "out", "per", "via", "using", "used", "use", "study", "studies", "results",
}


def _tokens(text: str) -> list[str]:
    return [
        w for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", text.lower())
        if w not in _STOPWORDS
    ]


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #

def extract_text(name: str, data: bytes) -> str:
    """Pull plain text out of one uploaded file. Returns "" if unreadable."""
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    if suffix == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:  # pragma: no cover - dependency is in requirements
            log.warning("pypdf is not installed; cannot read %s", name)
            return ""
        try:
            reader = PdfReader(io.BytesIO(data))
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001 - one bad page must not lose the file
                    continue
            return "\n".join(pages)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read PDF %s: %s", name, exc)
            return ""

    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def chunk_text(text: str) -> list[str]:
    """Split into overlapping word windows, preferring paragraph boundaries."""
    words = text.split()
    if not words:
        return []
    if len(words) <= CHUNK_WORDS:
        return [" ".join(words)]

    chunks: list[str] = []
    step = CHUNK_WORDS - CHUNK_OVERLAP
    for start in range(0, len(words), step):
        window = words[start : start + CHUNK_WORDS]
        if len(window) < 40 and chunks:
            break  # a tiny trailing remainder is already covered by the overlap
        chunks.append(" ".join(window))
    return chunks


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #

def rank_chunks(
    chunks: list[tuple[str, str, int]],
    query_terms: Iterable[str],
    *,
    limit: int,
) -> list[tuple[str, str, int, float]]:
    """BM25-lite scoring of (filename, text, index) chunks against the question.

    Classic TF-IDF with length normalisation. Transparent, instant, and explainable -
    a reviewer can see exactly which words earned a chunk its place.
    """
    wanted = Counter(w for term in query_terms for w in _tokens(term))
    if not wanted or not chunks:
        # No usable query terms: keep document order so the result is still stable.
        return [(name, text, idx, 0.0) for name, text, idx in chunks[:limit]]

    tokenised = [_tokens(text) for _, text, _ in chunks]
    total = len(chunks)
    lengths = [len(t) or 1 for t in tokenised]
    mean_length = sum(lengths) / total

    document_freq = Counter()
    for tokens in tokenised:
        for word in set(tokens):
            if word in wanted:
                document_freq[word] += 1

    k1, b = 1.5, 0.75
    scored: list[tuple[str, str, int, float]] = []

    for (name, text, idx), tokens, length in zip(chunks, tokenised, lengths):
        counts = Counter(tokens)
        score = 0.0
        for word, weight in wanted.items():
            freq = counts.get(word, 0)
            if not freq:
                continue
            idf = math.log(1 + (total - document_freq[word] + 0.5) / (document_freq[word] + 0.5))
            norm = freq * (k1 + 1) / (freq + k1 * (1 - b + b * length / mean_length))
            score += weight * idf * norm
        scored.append((name, text, idx, round(score, 3)))

    scored.sort(key=lambda row: -row[3])
    return [row for row in scored if row[3] > 0][:limit] or scored[:limit]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def build_records(
    files: list[tuple[str, bytes]],
    query_terms: Iterable[str],
    *,
    limit: int = 12,
) -> tuple[list[SourceRecord], list[str]]:
    """Turn uploads into ranked, citable corpus records.

    Returns (records, warnings). Never raises: an unreadable file becomes a warning,
    not a failed run.
    """
    warnings: list[str] = []
    all_chunks: list[tuple[str, str, int]] = []
    per_file: dict[str, int] = {}

    for name, data in files:
        suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if suffix not in SUPPORTED:
            warnings.append(f"{name}: unsupported file type (.{suffix}); skipped")
            continue

        text = clean_text(extract_text(name, data), limit=2_000_000)
        if len(text.split()) < 30:
            warnings.append(
                f"{name}: no extractable text (a scanned PDF needs OCR first); skipped"
            )
            continue

        pieces = chunk_text(text)
        per_file[name] = len(pieces)
        all_chunks.extend((name, piece, i) for i, piece in enumerate(pieces))

    if not all_chunks:
        return [], warnings

    ranked = rank_chunks(all_chunks, query_terms, limit=limit)

    records: list[SourceRecord] = []
    for name, text, index, score in ranked:
        total = per_file.get(name, 1)
        records.append(SourceRecord(
            sid="",  # minted by the pipeline
            kind="document",
            title=f"{name} (excerpt {index + 1} of {total})",
            url="",
            source="Uploaded document",
            snippet=clean_text(text, 3000),
            venue=name,
            identifiers={"file": name, "chunk": str(index + 1)},
            meta={
                "filename": name,
                "chunk_index": index,
                "chunk_count": total,
                "relevance_score": score,
                "user_supplied": True,
            },
            # A user-supplied document carries no peer review, so it is graded as
            # uncontrolled descriptive evidence unless appraisal says otherwise.
            design="other",
        ))

    if records:
        warnings.append(
            f"{len(records)} excerpt(s) from {len(per_file)} uploaded file(s) were added "
            "to the corpus. Uploaded documents are not peer reviewed and are graded "
            "accordingly."
        )

    return records, warnings
