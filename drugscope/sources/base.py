"""Shared plumbing for every evidence source.

Each source module is a thin, testable function over `Fetcher`. The fetcher owns
the three things that are easy to get wrong when you hammer six public research
APIs at once:

* **Politeness.** A per-host minimum interval, from `config.RATE_LIMITS`. NCBI in
  particular will start returning 429s without it.
* **Retry with backoff**, on connection errors, 429 and 5xx only.
* **A process-level response cache** with a TTL, so Streamlit's habit of
  re-running the whole script does not re-issue the same query.

Sources never raise into the pipeline. A stream that fails comes back empty and
contributes a warning, because a report built from five of six sources is far
more useful than a traceback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from html import unescape
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from ..config import (
    CACHE_TTL_SECONDS,
    DEFAULT_RATE_LIMIT,
    HTTP_RETRIES,
    HTTP_TIMEOUT,
    RATE_LIMITS,
    USER_AGENT,
)

log = logging.getLogger("drugscope.sources")

# Survives Streamlit reruns because it is module-level.
_CACHE: dict[str, tuple[float, Any]] = {}
# Last request time per host, shared by every run so politeness holds across them.
# The locks guarding it are NOT shared: each research run has its own event loop,
# and an asyncio.Lock is bound to the loop it was first used in - a module-level
# lock made every run after the first fail its queries with RuntimeError.
_HOST_LAST: dict[str, float] = {}


def _cache_key(url: str, params: dict[str, Any] | None) -> str:
    blob = url + "|" + json.dumps(params or {}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def clear_cache() -> None:
    _CACHE.clear()


class Fetcher:
    """One HTTP session for a whole research run."""

    def __init__(self, timeout: float = HTTP_TIMEOUT) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self.warnings: list[str] = []
        # Give-ups per host this run. A host that has failed twice is skipped for
        # the rest of the run rather than costing every remaining call a full
        # retry cycle - one outage should not add minutes to a report.
        self._failures: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def note(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
        log.warning(message)

    async def _throttle(self, host: str) -> None:
        interval = 1.0 / RATE_LIMITS.get(host, DEFAULT_RATE_LIMIT)
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = _HOST_LAST.get(host, 0.0) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _HOST_LAST[host] = time.monotonic()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        as_json: bool = True,
        label: str = "",
    ) -> Any:
        """GET with cache, throttle and retry. Returns None on give-up."""
        key = _cache_key(url, params)
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

        host = urlparse(url).netloc
        name = label or host
        last: Exception | None = None

        if self._failures.get(host, 0) >= 2:
            self.note(f"{host} is not responding; skipped its remaining queries in this run")
            return None

        for attempt in range(HTTP_RETRIES):
            await self._throttle(host)
            try:
                response = await self._client.get(url, params=params)
                if response.status_code == 404:
                    _CACHE[key] = (time.time(), None)
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code}", request=response.request, response=response
                    )
                response.raise_for_status()
                payload = response.json() if as_json else response.text
                _CACHE[key] = (time.time(), payload)
                return payload
            except (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                await asyncio.sleep(1.2 * (2**attempt))
            except json.JSONDecodeError as exc:
                last = exc
                break

        if not isinstance(last, json.JSONDecodeError):
            self._failures[host] = self._failures.get(host, 0) + 1
        self.note(f"{name} did not respond ({type(last).__name__}); that stream is incomplete")
        return None


# --------------------------------------------------------------------------- #
# Helpers shared by the parsers
# --------------------------------------------------------------------------- #

_TAG_RE = re.compile(r"<[^>]{1,80}>")


def clean_text(value: Any, limit: int = 2600) -> str:
    """Flatten whatever a research API calls a text field into one paragraph.

    Europe PMC returns abstracts with inline HTML section headers, openFDA label
    text carries stray replacement characters from its own upstream encoding, and
    several fields arrive as nested lists. All of that is normalised here so the
    analysis layer and the UI only ever see plain prose.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(clean_text(v, limit) for v in value)
    elif isinstance(value, dict):
        value = " ".join(clean_text(v, limit) for v in value.values())

    text = str(value)
    # Turn section markup into sentence breaks rather than deleting it outright.
    text = re.sub(r"</(h[1-6]|p|div|li|br)>", ". ", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    if "&" in text:
        text = unescape(text)
    # Replacement chars and control codes carry no information.
    text = text.replace("�", " ")
    text = "".join(ch if ch.isprintable() or ch.isspace() else " " for ch in text)
    text = re.sub(r"\s*\.\s*(?=\.)", "", text)  # collapse ". . ." from the above
    text = " ".join(text.split())

    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


def first_year(*candidates: Any) -> int | None:
    """Pull a plausible 4-digit year out of the first candidate that has one."""
    for candidate in candidates:
        if not candidate:
            continue
        match = re.search(r"(19|20)\d{2}", str(candidate))
        if match:
            year = int(match.group(0))
            if 1900 <= year <= 2100:
                return year
    return None


def dedupe(records: Iterable[Any]) -> list[Any]:
    """Drop duplicate records across overlapping literature sources.

    Europe PMC and PubMed index the same papers, so identity is checked on DOI,
    then PMID, then a normalised title.
    """
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    out: list[Any] = []

    for record in records:
        ids = {
            f"doi:{(record.identifiers.get('doi') or '').lower()}",
            f"pmid:{record.identifiers.get('pmid') or ''}",
            f"nct:{record.identifiers.get('nct') or ''}",
        }
        ids = {i for i in ids if not i.endswith(":")}
        title_key = "".join(ch for ch in record.title.lower() if ch.isalnum())[:90]

        if ids & seen_ids or (title_key and title_key in seen_titles):
            continue

        seen_ids |= ids
        if title_key:
            seen_titles.add(title_key)
        out.append(record)

    return out
