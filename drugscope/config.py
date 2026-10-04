"""Runtime configuration for DrugScope.

Everything tunable lives here so the UI, the retrieval layer and the model layer
read the same numbers. Values come from the environment where it makes sense
(API keys, contact email for NCBI) and from `ResearchDepth` where the trade-off
is "how much evidence do we pull and how hard do we think about it".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from dotenv import dotenv_values, find_dotenv, load_dotenv

load_dotenv()

APP_NAME = "DrugScope"
APP_TAGLINE = "Evidence-grade pharmaceutical intelligence"
APP_VERSION = "1.0.0"

# --- Models -----------------------------------------------------------------
# Claude Opus 5.5 everywhere by default. The bulk extractor runs one call per
# retrieved record, so it is the one place where a cheaper worker model is a
# defensible trade; it is exposed in the sidebar rather than chosen for you.
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"


def _env_model(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _claude_model(name: str) -> str:
    """A per-stage Claude override, ignored unless it actually names a Claude model.

    These variables only apply on the Anthropic path. A gateway id such as
    `vendor/model:free` set here would otherwise be sent to the Anthropic API and
    fail every call, so it is routed to `GATEWAY_MODEL_OVERRIDE` instead.
    """
    value = _env_model(name)
    return value if value.startswith("claude-") else DEFAULT_CLAUDE_MODEL


SYNTHESIS_MODEL = _claude_model("DRUGSCOPE_SYNTHESIS_MODEL")
EXTRACTION_MODEL = _claude_model("DRUGSCOPE_EXTRACTION_MODEL")
PLANNING_MODEL = _claude_model("DRUGSCOPE_PLANNING_MODEL")

WORKER_MODEL_CHOICES = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
if EXTRACTION_MODEL not in WORKER_MODEL_CHOICES:
    WORKER_MODEL_CHOICES.insert(0, EXTRACTION_MODEL)

# --- Providers ---------------------------------------------------------------
# Anthropic is the full-capability path (structured output, adaptive thinking,
# server-side web search). OpenRouter is an OpenAI-compatible gateway with a free
# model catalogue; it has none of those features, so the pipeline asks the backend
# what it supports rather than assuming.
PROVIDERS = {
    "anthropic": "Claude",
    "openai": "GPT",
    "openrouter": "OpenRouter",
}
PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
DEFAULT_PROVIDER = os.getenv("DRUGSCOPE_PROVIDER", "").strip().lower() or "anthropic"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "").strip()

# The model a gateway (OpenRouter / OpenAI) should start on. `OPENROUTER_MODEL` is
# the documented knob, but a gateway id placed in one of the per-stage Claude
# variables is honoured too, since that is the obvious place people put it.
GATEWAY_MODEL_OVERRIDE = OPENROUTER_MODEL or next(
    (
        _env_model(name)
        for name in (
            "DRUGSCOPE_SYNTHESIS_MODEL",
            "DRUGSCOPE_PLANNING_MODEL",
            "DRUGSCOPE_EXTRACTION_MODEL",
        )
        if "/" in _env_model(name)
    ),
    "",
)

# Published per-MTok list prices, used only for the in-app spend meter.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5-1": (10.00, 50.00),
    # OpenAI list prices; OpenRouter reports its own cost per call instead.
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
}

MAX_TOKENS_SYNTHESIS = 32000
MAX_TOKENS_EXTRACTION = 8000
MAX_TOKENS_PLANNING = 8000


class ResearchDepth(str, Enum):
    SCAN = "Scan"
    STANDARD = "Standard"
    DEEP = "Deep"


@dataclass(frozen=True)
class DepthProfile:
    """How much evidence to gather and how hard to reason over it."""

    label: str
    blurb: str
    papers: int           # max papers kept for full appraisal
    trials: int           # max clinical trials retrieved
    regulatory: int       # max regulatory records per stream
    effort: Literal["low", "medium", "high", "xhigh", "max"]
    extraction_effort: Literal["low", "medium", "high", "xhigh", "max"]
    concurrency: int      # simultaneous extraction calls
    web_search: bool      # use the server-side web_search tool for recent news
    est_minutes: str


DEPTH_PROFILES: dict[ResearchDepth, DepthProfile] = {
    ResearchDepth.SCAN: DepthProfile(
        label="Scan",
        blurb="Fast orientation pass. Top evidence only, no web sweep.",
        papers=12, trials=15, regulatory=6,
        effort="medium", extraction_effort="low",
        concurrency=6, web_search=False, est_minutes="~1-2 min",
    ),
    ResearchDepth.STANDARD: DepthProfile(
        label="Standard",
        blurb="Balanced review across literature, trials and regulators.",
        papers=28, trials=40, regulatory=12,
        effort="high", extraction_effort="medium",
        concurrency=8, web_search=True, est_minutes="~3-5 min",
    ),
    ResearchDepth.DEEP: DepthProfile(
        label="Deep",
        blurb="Exhaustive sweep with conflict adjudication and gap mapping.",
        papers=60, trials=80, regulatory=25,
        effort="max", extraction_effort="high",
        concurrency=10, web_search=True, est_minutes="~8-15 min",
    ),
}


# --- Retrieval --------------------------------------------------------------
# NCBI asks for a contact address and grants a higher rate limit with an API key.
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
NCBI_TOOL = "drugscope"
NCBI_EMAIL = os.getenv("NCBI_EMAIL", "").strip()

HTTP_TIMEOUT = 30.0
HTTP_RETRIES = 3
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (research tool)"

# Per-host politeness ceilings, requests/second.
RATE_LIMITS: dict[str, float] = {
    "eutils.ncbi.nlm.nih.gov": 9.0 if NCBI_API_KEY else 2.5,
    "www.ebi.ac.uk": 8.0,
    "clinicaltrials.gov": 5.0,
    "api.fda.gov": 3.0,
    "rxnav.nlm.nih.gov": 8.0,
}
DEFAULT_RATE_LIMIT = 4.0

CACHE_TTL_SECONDS = 60 * 60 * 6  # retrieval results are stable within a session


@dataclass
class SourceToggles:
    """Which evidence streams to query."""

    literature: bool = True
    trials: bool = True
    regulatory: bool = True
    pharmacology: bool = True
    recent_news: bool = True

    def as_list(self) -> list[str]:
        return [k for k, v in self.__dict__.items() if v]


@dataclass
class RunSettings:
    query: str
    depth: ResearchDepth = ResearchDepth.STANDARD
    toggles: SourceToggles = field(default_factory=SourceToggles)
    comparators: list[str] = field(default_factory=list)
    focus: str = ""
    year_from: int | None = None
    extraction_model: str = EXTRACTION_MODEL
    provider: str = DEFAULT_PROVIDER
    gateway_model: str = ""
    documents: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def profile(self) -> DepthProfile:
        return DEPTH_PROFILES[self.depth]


def openrouter_key_present() -> bool:
    return bool((os.getenv("OPENROUTER_API_KEY") or "").strip())


def _real_key(value: str | None) -> bool:
    """A set value that is not the `sk-...` placeholder from `.env.example`."""
    value = (value or "").strip()
    return bool(value) and not value.endswith("...") and value.lower() not in {"your-key", "changeme"}


def credentials_present(provider: str) -> bool:
    """True when a usable credential exists for this provider."""
    if provider == "anthropic":
        return api_key_present()
    env = PROVIDER_ENV.get(provider, "")
    return _real_key(os.getenv(env)) if env else False


def default_provider() -> str:
    """The provider the sidebar starts on.

    An explicit `DRUGSCOPE_PROVIDER` wins. Next, a key written in this project's
    `.env` beats one inherited from the machine environment - a global
    `OPENAI_API_KEY` set for some other tool should not outrank the key someone put
    in this project on purpose. Failing both, the first provider with any key.
    """
    if DEFAULT_PROVIDER in PROVIDERS and os.getenv("DRUGSCOPE_PROVIDER"):
        return DEFAULT_PROVIDER
    project = {k for k, v in dotenv_values(find_dotenv()).items() if (v or "").strip()}
    for key, env in PROVIDER_ENV.items():
        if env in project and credentials_present(key):
            return key
    return next((k for k in PROVIDERS if credentials_present(k)), "anthropic")


def misspelled_key_hints() -> list[str]:
    """Spot `.env` key names that are one typo away from one DrugScope reads.

    `OPENRUTER_API_KEY` looks right at a glance and silently leaves the app with
    no provider, so it is worth saying so rather than reporting "no key".
    """
    import difflib

    wanted = set(PROVIDER_ENV.values())
    hints: list[str] = []
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in wanted or not upper.endswith("_KEY") or not (value or "").strip():
            continue
        match = difflib.get_close_matches(upper, wanted, n=1, cutoff=0.85)
        if match and not (os.getenv(match[0]) or "").strip():
            hints.append(f"`{name}` looks like a typo for `{match[0]}`.")
    return hints


def api_key_present() -> bool:
    """True when some Anthropic credential is resolvable.

    An unset ANTHROPIC_API_KEY does not mean there are no credentials - the SDK
    also reads ANTHROPIC_AUTH_TOKEN and an `ant auth login` profile on disk.
    """
    if _real_key(os.getenv("ANTHROPIC_API_KEY")) or _real_key(os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return True
    for candidate in (
        os.path.expanduser("~/.config/anthropic"),
        os.path.join(os.getenv("APPDATA", ""), "anthropic") if os.getenv("APPDATA") else "",
    ):
        if candidate and os.path.isdir(candidate):
            return True
    return False
