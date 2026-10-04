"""Model providers.

DrugScope talks to two very different APIs behind one interface:

* **Anthropic** (`llm.Brain`) - the full-capability path. Structured output under a
  JSON schema, adaptive thinking, server-side web search, prompt caching.
* **OpenAI-compatible gateways** (`OpenAICompatBrain`) - OpenAI itself and
  OpenRouter, including OpenRouter's catalogue of free models. None of the
  Anthropic-specific features exist here, so this backend degrades deliberately
  rather than pretending.

The adaptations that make the free tier actually usable are worth calling out,
because they are not cosmetic:

1. **A live model catalogue.** Free models on OpenRouter come and go every few
   months, so a hard-coded list is a list of 404s waiting to happen. The catalogue
   is read from OpenRouter's public `/models` endpoint, ranked, and cached; the
   static list below is only the offline fallback.
2. **The strongest JSON mode the model supports.** `json_schema` where the model
   advertises structured outputs, `json_object` where it only has JSON mode, and the
   schema in the prompt everywhere. A model that rejects a mode is stepped down one
   level for the rest of the run, and `coerce_json` recovers the object from
   whatever wrapping the model added.
3. **Batched appraisal.** Free models are rate-limited per minute *and* per day. One
   call per record would spend a whole daily quota on a single Standard run, so this
   backend appraises several records per call and the pipeline asks the backend how
   many it wants.
4. **Low concurrency and 429-aware retry.** Free endpoints reject bursts, so the
   semaphore is small and rate-limit responses back off rather than fail the run -
   except a spent *daily* quota, which no amount of waiting fixes within a run.

The pipeline never branches on provider. It asks the backend for its capabilities and
adapts, so adding another gateway means adding a row to `GATEWAYS` and nothing else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import MODEL_PRICES
from .llm import Brain, Capabilities, LLMError, ThinkingSink, coerce_json, strict_schema
from .models import Usage

log = logging.getLogger("drugscope.providers")

T = TypeVar("T", bound=BaseModel)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Per-provider wiring for the OpenAI-compatible backend. Adding another gateway
# is a row here, not a new class.
GATEWAYS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "url": OPENROUTER_URL,
        "env": "OPENROUTER_API_KEY",
        "label": "OpenRouter",
        "console": "https://openrouter.ai/keys",
        # OpenRouter still takes the classic parameter for every model.
        "max_tokens_param": "max_tokens",
    },
    "openai": {
        "url": OPENAI_URL,
        "env": "OPENAI_API_KEY",
        "label": "OpenAI",
        "console": "https://platform.openai.com/api-keys",
        # Accepted by every current OpenAI chat model; `max_tokens` is rejected
        # by the reasoning families.
        "max_tokens_param": "max_completion_tokens",
    },
}

OPENAI_MODELS = [
    "gpt-4o-mini",
    "gpt-4.1-mini",
    "gpt-5-mini",
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
]

# Offline fallback only - the sidebar reads the live catalogue. These were free on
# OpenRouter and advertised JSON output when this list was last refreshed.
FREE_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-31b-it:free",
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-26b-a4b-it:free",
    "dots-studio/dots-3-note-preview:free",
]

# Paid routes worth suggesting when free-tier quality or quota is the bottleneck.
PAID_SUGGESTIONS = [
    "anthropic/claude-sonnet-5-5",
    "openai/gpt-4o-mini",
    "google/gemini-2.5-flash",
]

# Free entries that are not general text models - classifiers, coders, audio.
_NOT_GENERAL = re.compile(r"safety|guard|moderation|embed|tts|whisper|audio|code|coder|laguna", re.I)
_SMALL_HINT = re.compile(r"nano|lightning|tiny|mini|small|flash-lite", re.I)

_CATALOGUE: dict[str, Any] = {"at": 0.0, "models": [], "refreshing": False}
_CATALOGUE_TTL = 60 * 30
_CATALOGUE_FILE = Path(__file__).resolve().parents[1] / "data" / "openrouter_free_models.json"
_catalogue_lock = threading.Lock()

# How hard a reasoning model may think, by the effort a stage asks for. Hidden
# reasoning is most of the wait on free models - one appraisal measured 24.7 s with
# the model's default reasoning and 4.9 s with it off, with valid output both times -
# so bulk appraisal (low effort) runs without it and only synthesis thinks hard.
_REASONING = {
    "low": {"enabled": False},
    "medium": {"effort": "low", "exclude": True},
    "high": {"effort": "medium", "exclude": True},
    "xhigh": {"effort": "medium", "exclude": True},
    "max": {"effort": "high", "exclude": True},
}


def gateway_key(provider: str) -> str:
    env = GATEWAYS.get(provider, {}).get("env", "")
    return (os.getenv(env) or "").strip() if env else ""


def openrouter_key() -> str:
    return gateway_key("openrouter")


# --------------------------------------------------------------------------- #
# OpenRouter catalogue
# --------------------------------------------------------------------------- #

def _size_in_billions(model_id: str) -> float:
    """Largest `NNb` figure in an id - `nemotron-3-super-120b-a12b` -> 120."""
    sizes = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)b(?![a-z])", model_id.lower())]
    return max(sizes) if sizes else 0.0


def _rank(entry: dict[str, Any]) -> float:
    """Higher is better: JSON support first, then model size, then context."""
    score = 0.0
    if entry["json_schema"]:
        score += 3
    elif entry["json_object"]:
        score += 2
    size = _size_in_billions(entry["id"])
    score += min(math.log10(size + 1), 2.5) if size else 1.0
    if _SMALL_HINT.search(entry["id"]):
        score -= 1.0
    score += min((entry["context"] or 0) / 1_000_000, 0.5)
    return score


def free_model_catalogue(*, refresh: bool = False, wait: bool = False) -> list[dict[str, Any]]:
    """The free text models OpenRouter serves right now, best first.

    Each entry: `id`, `name`, `context`, `max_output`, `json_schema`, `json_object`,
    `reasoning`. Never raises and, unless `wait=True`, never blocks: it answers from
    memory, then from the copy saved on disk by the last fetch, then from the static
    `FREE_MODELS` list, and refreshes from OpenRouter in a background thread when the
    answer is stale. Page loads therefore never wait on OpenRouter.
    """
    fresh = time.time() - _CATALOGUE["at"] < _CATALOGUE_TTL
    if _CATALOGUE["models"] and fresh and not refresh:
        return _CATALOGUE["models"]

    if not _CATALOGUE["models"]:
        _load_disk_catalogue()
    if wait or refresh:
        _fetch_catalogue()
    else:
        _refresh_in_background()
    return _CATALOGUE["models"] or _static_catalogue()


def _static_catalogue() -> list[dict[str, Any]]:
    return [
        {"id": m, "name": m, "context": 0, "max_output": 0,
         "json_schema": False, "json_object": True, "reasoning": False}
        for m in FREE_MODELS
    ]


def _load_disk_catalogue() -> None:
    try:
        saved = json.loads(_CATALOGUE_FILE.read_text(encoding="utf-8"))
        if saved.get("models"):
            # Treat the saved copy as stale so a background refresh still runs.
            _CATALOGUE.update(at=0.0, models=saved["models"])
    except (OSError, ValueError):
        pass


def _refresh_in_background() -> None:
    with _catalogue_lock:
        if _CATALOGUE["refreshing"]:
            return
        _CATALOGUE["refreshing"] = True
    threading.Thread(target=_fetch_catalogue, name="openrouter-catalogue", daemon=True).start()


def _fetch_catalogue() -> None:
    try:
        _fetch_catalogue_now()
    finally:
        _CATALOGUE["refreshing"] = False


def _fetch_catalogue_now() -> None:
    models: list[dict[str, Any]] = []
    try:
        response = httpx.get(OPENROUTER_MODELS_URL, timeout=10.0)
        response.raise_for_status()
        for item in response.json().get("data") or []:
            model_id = str(item.get("id") or "")
            if not model_id.endswith(":free") or _NOT_GENERAL.search(model_id):
                continue
            arch = item.get("architecture") or {}
            if "text" not in (arch.get("output_modalities") or ["text"]):
                continue
            params = set(item.get("supported_parameters") or [])
            top = item.get("top_provider") or {}
            models.append({
                "id": model_id,
                "name": str(item.get("name") or model_id),
                "context": int(item.get("context_length") or 0),
                "max_output": int(top.get("max_completion_tokens") or 0),
                "json_schema": "structured_outputs" in params,
                "json_object": "response_format" in params,
                "reasoning": "reasoning" in params,
            })
    except Exception as exc:  # noqa: BLE001 - the catalogue is a convenience
        log.warning("OpenRouter catalogue unavailable: %s", exc)

    if not models:
        # Keep whatever we had; retry the live list in a minute.
        _CATALOGUE["at"] = time.time() - _CATALOGUE_TTL + 60
        return

    models.sort(key=_rank, reverse=True)
    _CATALOGUE.update(at=time.time(), models=models)
    try:
        _CATALOGUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CATALOGUE_FILE.write_text(json.dumps({"saved_at": time.time(), "models": models}), encoding="utf-8")
    except OSError:
        pass


def model_info(model_id: str) -> dict[str, Any] | None:
    """Catalogue entry for a model id, if it is a known free model."""
    return next((m for m in _CATALOGUE["models"] if m["id"] == model_id), None)


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #

# JSON modes, strongest first. A rejection steps down one level.
_JSON_MODES = ("schema", "object", "none")


class OpenAICompatBrain:
    """One backend for every OpenAI-compatible gateway (OpenAI, OpenRouter, ...).

    The shape of the request is identical; only the base URL, the key and the
    rate-limit behaviour differ, so the differences live in `GATEWAYS` rather than
    in a subclass per vendor.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str = "",
        concurrency: int = 2,
        max_retries: int = 4,
        temperature: float = 0.2,
    ) -> None:
        gateway = GATEWAYS.get(provider)
        if gateway is None:
            raise LLMError(f"unknown provider: {provider}")

        self.provider = provider
        self.gateway = gateway
        self.url = gateway["url"]
        self.model = model
        self.api_key = api_key or gateway_key(provider)
        if not self.api_key:
            raise LLMError(
                f"{gateway['env']} is not set. Add it to the .env file in the project "
                "root, then reload the page."
            )

        self.usage = Usage()
        self.caps = Capabilities(
            structured_output=False,     # no output_config; schema goes in the prompt
            adaptive_thinking=False,
            effort=False,
            refusal_fallbacks=False,
        )
        self.max_retries = max_retries
        self.temperature: float | None = temperature
        self._gate = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()

        info = model_info(model) if provider == "openrouter" else None
        if info is not None:
            self._json_mode = "schema" if info["json_schema"] else "object" if info["json_object"] else "none"
            self._max_output = info["max_output"]
            self._reasoning = bool(info.get("reasoning"))
        else:
            # OpenAI models all accept JSON mode; an unknown gateway model is
            # probed with it and stepped down on rejection.
            self._json_mode = "object"
            self._max_output = 0
            self._reasoning = False

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if provider == "openrouter":
            # OpenRouter uses these for attribution on its dashboard.
            headers["HTTP-Referer"] = "https://github.com/drugscope"
            headers["X-Title"] = "DrugScope"

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=20.0), headers=headers,
        )

    # -- capabilities the pipeline asks about ------------------------------
    @property
    def supports_web_search(self) -> bool:
        """Server-side search is an Anthropic tool; OpenRouter's is a paid plugin."""
        return False

    @property
    def appraisal_batch_size(self) -> int:
        """Records per appraisal call.

        A free endpoint is capped per day, so one call per record would spend the
        whole quota on a single run; a paid key has no such cliff and gets the
        higher-quality smaller batches.
        """
        return 6 if self.is_free_tier else 3

    @property
    def is_free_tier(self) -> bool:
        return self.model.endswith(":free")

    # -- accounting ---------------------------------------------------------
    async def _record_usage(self, payload: dict[str, Any], model: str) -> None:
        usage = payload.get("usage") or {}
        inp = int(usage.get("prompt_tokens") or 0)
        out = int(usage.get("completion_tokens") or 0)
        # OpenRouter reports what it charged; OpenAI does not, so list prices
        # stand in for the spend meter there.
        cost = float(usage.get("cost") or 0.0)
        if not cost and model in MODEL_PRICES and not model.endswith(":free"):
            in_price, out_price = MODEL_PRICES[model]
            cost = inp / 1_000_000 * in_price + out / 1_000_000 * out_price
        async with self._lock:
            self.usage.add(Usage(input_tokens=inp, output_tokens=out, calls=1, cost_usd=cost))

    # -- request construction -----------------------------------------------
    def _limit(self, max_tokens: int) -> int:
        return min(max_tokens, self._max_output) if self._max_output else max_tokens

    def _body(self, model: str, messages: list[dict[str, str]], max_tokens: int,
              effort: str = "high") -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            self.gateway["max_tokens_param"]: self._limit(max_tokens),
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.provider == "openrouter":
            body["usage"] = {"include": True}
            if self._reasoning and effort in _REASONING:
                body["reasoning"] = dict(_REASONING[effort])
        return body

    def _apply_json_mode(self, body: dict[str, Any], schema: dict[str, Any], name: str) -> None:
        body.pop("response_format", None)
        if self._json_mode == "schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": name, "strict": True, "schema": schema},
            }
        elif self._json_mode == "object":
            body["response_format"] = {"type": "json_object"}

    def _step_down_json(self, reason: str) -> bool:
        index = _JSON_MODES.index(self._json_mode)
        if index + 1 >= len(_JSON_MODES):
            return False
        self._json_mode = _JSON_MODES[index + 1]
        self.caps.notes.append(
            f"{self.model} rejected {_JSON_MODES[index]} JSON mode ({reason}); "
            f"continuing with {self._json_mode}"
        )
        return True

    # -- transport ----------------------------------------------------------
    async def _post(
        self,
        body: dict[str, Any],
        *,
        schema: dict[str, Any] | None = None,
        name: str = "response",
    ) -> dict[str, Any]:
        last: Exception | None = None
        label = self.gateway["label"]

        for attempt in range(self.max_retries + 2):
            if schema is not None:
                self._apply_json_mode(body, schema, name)
            try:
                response = await self._client.post(self.url, json=body)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last = exc
                await asyncio.sleep(3.0 * (attempt + 1))
                continue

            text = response.text[:600]
            low = text.lower()

            if response.status_code == 429:
                if "per-day" in low or "per day" in low or "daily" in low:
                    raise LLMError(
                        f"{label}'s daily free-model quota is used up for this key. It "
                        "resets every 24 hours; adding a little credit to the account "
                        "raises the limit, or switch to a paid model in the sidebar."
                    )
                # Per-minute limits: honour Retry-After and back off.
                delay = float(response.headers.get("retry-after", 0) or 0) or 12.0 * (attempt + 1)
                log.warning("%s rate limited; waiting %.0fs", self.provider, delay)
                await asyncio.sleep(min(delay, 90.0))
                last = LLMError("rate limited by the provider")
                continue

            if response.status_code in (400, 404, 422) and schema is not None and self._json_mode != "none":
                # A model or route that cannot do this JSON mode - OpenRouter says
                # so as 404 "no endpoints ... requested parameters", others as 400.
                if any(k in low for k in ("response_format", "json", "schema", "parameter", "structured")):
                    if self._step_down_json(f"HTTP {response.status_code}"):
                        continue

            if response.status_code == 400 and "temperature" in low and "temperature" in body:
                # Reasoning models accept only their default sampling.
                body.pop("temperature", None)
                self.temperature = None
                continue

            if response.status_code in (400, 404) and "reasoning" in low and "reasoning" in body:
                # This route will not take a reasoning setting; run with its default.
                body.pop("reasoning", None)
                self._reasoning = False
                self.caps.notes.append(f"{self.model} ignored the reasoning setting; ran with its default")
                continue

            if response.status_code >= 500:
                last = LLMError(f"upstream {response.status_code}")
                await asyncio.sleep(4.0 * (attempt + 1))
                continue

            if response.status_code in (401, 403):
                # By far the most common setup failure - say what to do about it
                # rather than echoing a provider stack trace.
                raise LLMError(
                    f"{label} rejected the API key ({response.status_code}). Check "
                    f"{self.gateway['env']} in your .env file, then reload. Keys are "
                    f"managed at {self.gateway['console']}."
                )

            if response.status_code == 402:
                raise LLMError(
                    f"{label} reports no credit on this key. Top it up, or switch to a "
                    "free model in the sidebar."
                )

            if response.status_code == 404:
                if "data policy" in low or "privacy" in low:
                    raise LLMError(
                        f"{label} has no endpoint for '{self.model}' under this account's "
                        "privacy settings. Free models need 'Enable free endpoints that may "
                        "publish prompts' at https://openrouter.ai/settings/privacy."
                    )
                raise LLMError(
                    f"{label} does not serve the model '{self.model}' any more. Pick "
                    f"another in the sidebar. ({text[:200]})"
                )

            if response.status_code >= 400:
                raise LLMError(f"{label} {response.status_code}: {text[:400]}")

            try:
                payload = response.json()
            except json.JSONDecodeError:
                last = LLMError("provider returned a non-JSON body")
                await asyncio.sleep(2.0 * (attempt + 1))
                continue

            # OpenRouter reports upstream provider failures inside a 200 body.
            if payload.get("error"):
                error = payload["error"]
                message = str(error.get("message", error) if isinstance(error, dict) else error)[:300]
                if "rate" in message.lower():
                    await asyncio.sleep(12.0 * (attempt + 1))
                    last = LLMError(message)
                    continue
                raise LLMError(f"{label} provider error: {message}")

            await self._record_usage(payload, str(body.get("model") or self.model))
            return payload

        raise LLMError(f"{label} request failed after {self.max_retries} attempts: {last}")

    def _text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"{self.provider} returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # Some providers return content as a block list.
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not content:
            reason = choices[0].get("finish_reason", "unknown")
            hint = (
                " - the model spent its output budget reasoning; try a non-reasoning model"
                if reason == "length" else ""
            )
            raise LLMError(f"{self.provider} returned empty content (finish_reason={reason}){hint}")
        return str(content)

    # -- public API ---------------------------------------------------------
    async def structured(
        self,
        *,
        model: str | None = None,
        schema_model: type[T],
        system: str,
        user: str,
        max_tokens: int,
        effort: str = "high",
        cache_system: bool = True,
        tools: list[dict[str, Any]] | None = None,
        on_thinking: ThinkingSink = None,
    ) -> T:
        schema = strict_schema(schema_model)
        schema_text = json.dumps(schema, separators=(",", ":"))
        last_error: Exception | None = None

        async with self._gate:
            for attempt in range(self.max_retries):
                instruction = (
                    f"{user}\n\n"
                    "Respond with ONE JSON object and nothing else. No prose before or "
                    "after it, no markdown code fence, no explanation. Every property in "
                    "the schema is required - use an empty string or an empty array "
                    "where you have nothing, and -1 for an unknown number. It must "
                    f"validate against this JSON Schema:\n{schema_text}"
                )
                if attempt and last_error is not None:
                    instruction += (
                        f"\n\nYour previous reply was rejected: {last_error}\n"
                        "Return corrected JSON satisfying every required field."
                    )

                body = self._body(
                    model or self.model,
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": instruction},
                    ],
                    max_tokens,
                    effort,
                )

                try:
                    payload = await self._post(body, schema=schema, name=schema_model.__name__)
                    text = self._text(payload)
                    return schema_model.model_validate(coerce_json(text))
                except (ValidationError, ValueError) as exc:
                    last_error = exc
                    log.warning("%s structured attempt %d failed: %s", self.provider, attempt + 1, exc)
                    await asyncio.sleep(1.5 * (attempt + 1))
                except LLMError as exc:
                    # Configuration problems will not fix themselves on retry.
                    if any(k in str(exc) for k in ("API key", "quota", "credit", "does not serve", "privacy")):
                        raise
                    last_error = exc
                    await asyncio.sleep(2.0 * (attempt + 1))

        raise LLMError(
            f"{schema_model.__name__} could not be produced by {self.model}: {last_error}"
        )

    async def prose(
        self,
        *,
        model: str | None = None,
        system: str,
        user: str,
        max_tokens: int,
        effort: str = "high",
        tools: list[dict[str, Any]] | None = None,
        on_thinking: ThinkingSink = None,
    ) -> str:
        async with self._gate:
            payload = await self._post(self._body(
                model or self.model,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens,
                effort,
            ))
            return self._text(payload)

    async def with_server_tools(self, **kwargs: Any) -> Any:
        """No server-side tools on this backend; the pipeline checks first."""
        raise LLMError(f"server-side tools are not available on {self.gateway['label']}")

    async def close(self) -> None:
        await self._client.aclose()


# Kept so existing imports keep working.
OpenRouterBrain = OpenAICompatBrain


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

def make_brain(
    *,
    provider: str,
    model: str = "",
    concurrency: int = 8,
) -> Any:
    """Return the backend for the chosen provider."""
    if provider in GATEWAYS:
        if provider == "openrouter":
            # Never blocks: memory, disk copy or static list, so per-model limits are known.
            chosen = model or free_model_catalogue()[0]["id"]
        else:
            chosen = model or OPENAI_MODELS[0]
        return OpenAICompatBrain(
            provider=provider,
            model=chosen,
            # Free endpoints allow ~20 requests a minute; four calls in flight stays
            # well inside that while halving the wait against two.
            concurrency=4 if chosen.endswith(":free") else min(concurrency, 6),
        )
    return Brain(concurrency=concurrency)
