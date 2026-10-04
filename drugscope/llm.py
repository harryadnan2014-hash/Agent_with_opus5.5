"""The model layer.

One job: turn a prompt plus a Pydantic class into a validated instance of that
class, as cheaply and as reliably as possible.

Design notes worth knowing before you edit this file:

* **Structured output, not prose parsing.** Every analytical call goes out with
  `output_config.format = {"type": "json_schema", ...}`, so the response is
  schema-valid JSON rather than markdown we have to scrape.
* **Streaming always.** These calls have large inputs and large `max_tokens`;
  streaming is what keeps them under the HTTP timeout.
* **Adaptive thinking with summaries on.** `display: "summarized"` is what lets
  the UI show live reasoning instead of a long silent pause.
* **Refusal fallbacks on by default** for Opus-class models, so a policy decline
  on a clinical-safety question gets re-run on a fallback model inside the same
  call instead of failing the run.
* **Capability probing.** `fallbacks`, `output_config` and adaptive thinking are
  recent additions. If the installed SDK or endpoint rejects one, the flag is
  turned off for the rest of the process and the call is retried without it -
  degraded, but still working, and the degradation is reported to the UI.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel, ValidationError


class _LazySDK:
    """`anthropic`, imported on first use.

    The SDK takes ~2 seconds to import, and an OpenRouter or OpenAI setup never needs
    it - so it must not sit on every app start. Attribute access (including the
    exception classes in `except` clauses, which are only evaluated when an exception
    is being matched) loads it then.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("anthropic"), name)


anthropic = _LazySDK()

from .config import MODEL_PRICES
from .models import Usage

log = logging.getLogger("drugscope.llm")

T = TypeVar("T", bound=BaseModel)

ThinkingSink = Callable[[str], None] | None


# --------------------------------------------------------------------------- #
# JSON schema
# --------------------------------------------------------------------------- #

def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic -> a JSON schema the API will accept in strict mode.

    Strict json_schema output requires, for every object in the tree, that
    `additionalProperties` is false and that `required` lists every property.
    Pydantic emits neither by default, so we walk the tree and add them. This is
    also why the analysis models in `models.py` have no optional fields.
    """
    schema = model.model_json_schema()

    def harden(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                props = node.get("properties")
                if isinstance(props, dict):
                    node["additionalProperties"] = False
                    node["required"] = list(props.keys())
            # `title` and `default` carry no meaning for the model and only
            # inflate the prompt.
            node.pop("title", None)
            node.pop("default", None)
            for value in node.values():
                harden(value)
        elif isinstance(node, list):
            for item in node:
                harden(item)

    harden(schema)
    return schema


def coerce_json(text: str) -> Any:
    """Best-effort JSON recovery for the degraded path.

    Only used when the endpoint refused `output_config` and we had to ask for
    JSON in the prompt instead.
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost balanced object.
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in model response")
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("unterminated JSON object in model response")


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #

@dataclass
class Capabilities:
    """Runtime feature flags, switched off on first rejection."""

    structured_output: bool = True
    adaptive_thinking: bool = True
    effort: bool = True
    refusal_fallbacks: bool = True
    notes: list[str] = field(default_factory=list)

    def disable(self, name: str, reason: str) -> None:
        if getattr(self, name, False):
            setattr(self, name, False)
            note = f"{name} unavailable ({reason}); continuing without it"
            self.notes.append(note)
            log.warning(note)


_UNSUPPORTED_HINTS = {
    "structured_output": ("output_config", "json_schema", "format"),
    "adaptive_thinking": ("thinking", "adaptive", "budget_tokens"),
    "effort": ("effort",),
    "refusal_fallbacks": ("fallbacks", "server-side-fallback", "betas"),
}


def _blamed_feature(message: str, caps: Capabilities) -> str | None:
    """The still-enabled feature a rejection message points at, if any.

    Only enabled features are candidates: `output_config` carries both the schema
    and the effort level, so once structured output is off a second complaint
    about `output_config` must be blamed on effort, not on the same flag again.
    """
    low = message.lower()
    for feature, hints in _UNSUPPORTED_HINTS.items():
        if getattr(caps, feature, False) and any(h in low for h in hints):
            return feature
    return None


# Models that take adaptive thinking and `output_config.effort`. Haiku 4.5 and the
# pre-4.6 generation reject both, so a cheap worker model must not switch them off
# for the planning and synthesis calls that share the same backend.
_ADAPTIVE_PREFIXES = (
    "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-sonnet-4-6",
)
# Models the server-side refusal fallback ("default" form) is offered on.
_FALLBACK_PREFIXES = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")


def _model_supports_adaptive(model: str) -> bool:
    return model.startswith(_ADAPTIVE_PREFIXES)


def _friendly(exc: Exception, model: str) -> LLMError | None:
    """Turn the setup failures people actually hit into an instruction."""
    if isinstance(exc, anthropic.AuthenticationError):
        return LLMError(
            "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in your .env "
            "file (or run `ant auth login`), then reload."
        )
    if isinstance(exc, anthropic.PermissionDeniedError):
        return LLMError(f"This Anthropic key is not permitted to use {model}.")
    if isinstance(exc, anthropic.NotFoundError):
        return LLMError(
            f"Anthropic does not recognise the model '{model}'. Check the "
            "DRUGSCOPE_*_MODEL values in .env or pick another worker model."
        )
    return None


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class LLMError(RuntimeError):
    pass


class RefusalError(LLMError):
    """The model, and any fallback, declined the request."""


class Brain:
    """Async wrapper over the Messages API, with accounting and a concurrency gate."""

    def __init__(
        self,
        *,
        concurrency: int = 8,
        max_retries: int = 3,
        caps: Capabilities | None = None,
    ) -> None:
        self.client = anthropic.AsyncAnthropic(max_retries=2, timeout=900.0)
        self.caps = caps or Capabilities()
        self.usage = Usage()
        self.max_retries = max_retries
        self._gate = asyncio.Semaphore(concurrency)
        self._lock = asyncio.Lock()

    provider = "anthropic"

    # -- capabilities the pipeline asks about ------------------------------
    @property
    def supports_web_search(self) -> bool:
        return True

    @property
    def appraisal_batch_size(self) -> int:
        """One record per call: the whole point of stage 2 is undivided attention."""
        return 1

    @property
    def is_free_tier(self) -> bool:
        return False

    # -- accounting ---------------------------------------------------------
    async def _record_usage(self, model: str, raw: Any) -> None:
        inp = int(getattr(raw, "input_tokens", 0) or 0)
        out = int(getattr(raw, "output_tokens", 0) or 0)
        cread = int(getattr(raw, "cache_read_input_tokens", 0) or 0)
        cwrite = int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
        in_price, out_price = MODEL_PRICES.get(model, (5.00, 25.00))
        cost = (
            (inp / 1_000_000) * in_price
            + (out / 1_000_000) * out_price
            + (cread / 1_000_000) * in_price * 0.1
            + (cwrite / 1_000_000) * in_price * 1.25
        )
        async with self._lock:
            self.usage.add(
                Usage(
                    input_tokens=inp,
                    output_tokens=out,
                    cache_read_tokens=cread,
                    cache_write_tokens=cwrite,
                    calls=1,
                    cost_usd=cost,
                )
            )

    # -- request construction ----------------------------------------------
    def _build(
        self,
        *,
        model: str,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        max_tokens: int,
        effort: str,
        schema: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        want_thinking: bool,
    ) -> tuple[dict[str, Any], bool]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }

        adaptive = _model_supports_adaptive(model)

        output_config: dict[str, Any] = {}
        if self.caps.effort and adaptive:
            output_config["effort"] = effort
        if schema is not None and self.caps.structured_output:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        if output_config:
            kwargs["output_config"] = output_config

        if self.caps.adaptive_thinking and adaptive:
            kwargs["thinking"] = {
                "type": "adaptive",
                "display": "summarized" if want_thinking else "omitted",
            }

        if tools:
            kwargs["tools"] = tools

        use_beta = False
        if self.caps.refusal_fallbacks and model.startswith(_FALLBACK_PREFIXES):
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            use_beta = True

        return kwargs, use_beta

    async def _stream_once(
        self,
        kwargs: dict[str, Any],
        use_beta: bool,
        on_thinking: ThinkingSink,
    ) -> tuple[str, Any]:
        """One streamed request. Returns (text, final_message)."""
        namespace = self.client.beta.messages if use_beta else self.client.messages
        async with namespace.stream(**kwargs) as stream:
            if on_thinking is not None:
                async for event in stream:
                    if (
                        getattr(event, "type", "") == "content_block_delta"
                        and getattr(getattr(event, "delta", None), "type", "") == "thinking_delta"
                    ):
                        chunk = getattr(event.delta, "thinking", "") or ""
                        if chunk:
                            on_thinking(chunk)
            final = await stream.get_final_message()

        if getattr(final, "stop_reason", None) == "refusal":
            details = getattr(final, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise RefusalError(f"request declined (category: {category})")

        text = "".join(
            block.text for block in final.content if getattr(block, "type", "") == "text"
        )
        return text, final

    # -- public API ---------------------------------------------------------
    async def structured(
        self,
        *,
        model: str,
        schema_model: type[T],
        system: str,
        user: str,
        max_tokens: int,
        effort: str = "high",
        cache_system: bool = True,
        tools: list[dict[str, Any]] | None = None,
        on_thinking: ThinkingSink = None,
    ) -> T:
        """Run a call and return a validated `schema_model` instance."""
        schema = strict_schema(schema_model)
        system_blocks: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache_system:
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}

        last_error: Exception | None = None
        attempt = 0
        # Each capability can be switched off at most once, so this bounds the
        # retries that downgrades add on top of `max_retries`.
        downgrades_left = 4

        async with self._gate:
            while attempt < self.max_retries:
                prompt = user
                if not self.caps.structured_output:
                    prompt = (
                        f"{user}\n\n"
                        "Reply with a single JSON object and nothing else - no prose, no code fence. "
                        "It must validate against this JSON Schema:\n"
                        f"{json.dumps(schema, separators=(',', ':'))}"
                    )
                if attempt and last_error is not None:
                    prompt += (
                        f"\n\nYour previous reply could not be used: {last_error}. "
                        "Return corrected JSON that satisfies every required field."
                    )

                kwargs, use_beta = self._build(
                    model=model,
                    system=system_blocks,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    effort=effort,
                    schema=schema,
                    tools=tools,
                    want_thinking=on_thinking is not None,
                )

                try:
                    text, final = await self._stream_once(kwargs, use_beta, on_thinking)
                    await self._record_usage(model, getattr(final, "usage", None))
                    if getattr(final, "stop_reason", None) == "max_tokens":
                        raise LLMError("response hit max_tokens before the JSON closed")
                    payload = coerce_json(text)
                    return schema_model.model_validate(payload)

                except RefusalError:
                    raise
                except TypeError as exc:
                    # The installed SDK does not know one of the newer kwargs.
                    feature = _blamed_feature(str(exc), self.caps)
                    if feature and downgrades_left:
                        downgrades_left -= 1
                        self.caps.disable(feature, "not supported by the installed SDK")
                        continue
                    raise
                except anthropic.BadRequestError as exc:
                    feature = _blamed_feature(getattr(exc, "message", "") or str(exc), self.caps)
                    if feature and downgrades_left:
                        downgrades_left -= 1
                        self.caps.disable(feature, "rejected by the API")
                        continue
                    raise
                except (ValidationError, ValueError, LLMError) as exc:
                    last_error = exc
                    log.warning("structured call attempt %d failed: %s", attempt + 1, exc)
                    await asyncio.sleep(1.5 * (attempt + 1))
                except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
                    last_error = exc
                    await asyncio.sleep(4.0 * (attempt + 1))
                except anthropic.APIStatusError as exc:
                    friendly = _friendly(exc, model)
                    if friendly is not None:
                        raise friendly from exc
                    if exc.status_code >= 500:
                        last_error = exc
                        await asyncio.sleep(4.0 * (attempt + 1))
                    else:
                        raise
                attempt += 1

        raise LLMError(f"{schema_model.__name__} could not be produced after {self.max_retries} attempts: {last_error}")

    async def prose(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        effort: str = "high",
        tools: list[dict[str, Any]] | None = None,
        on_thinking: ThinkingSink = None,
    ) -> str:
        """A plain text call - used for the web sweep, where tools drive the turn."""
        attempt = 0
        downgrades_left = 4
        async with self._gate:
            while attempt < self.max_retries:
                # Rebuilt every pass so a capability switched off below takes effect.
                kwargs, use_beta = self._build(
                    model=model,
                    system=[{"type": "text", "text": system}],
                    messages=[{"role": "user", "content": user}],
                    max_tokens=max_tokens,
                    effort=effort,
                    schema=None,
                    tools=tools,
                    want_thinking=on_thinking is not None,
                )
                try:
                    text, final = await self._stream_once(kwargs, use_beta, on_thinking)
                    await self._record_usage(model, getattr(final, "usage", None))
                    return text
                except RefusalError:
                    raise
                except (TypeError, anthropic.BadRequestError) as exc:
                    feature = _blamed_feature(getattr(exc, "message", "") or str(exc), self.caps)
                    if feature and downgrades_left:
                        downgrades_left -= 1
                        self.caps.disable(feature, "rejected for this request")
                        continue
                    raise
                except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError):
                    await asyncio.sleep(4.0 * (attempt + 1))
                except anthropic.APIStatusError as exc:
                    friendly = _friendly(exc, model)
                    if friendly is not None:
                        raise friendly from exc
                    raise
                attempt += 1
            raise LLMError("prose call failed after retries")

    async def with_server_tools(
        self,
        *,
        model: str,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        max_tokens: int,
        effort: str = "high",
        max_resumes: int = 4,
        on_thinking: ThinkingSink = None,
    ) -> Any:
        """Run a server-tool turn and return the final message object.

        The caller gets the whole message, not just text, because the web-search
        result blocks carry the real URLs and titles - the only way to build
        citable records from a web sweep instead of trusting the model to repeat
        links correctly.

        Server-tool turns can stop with `pause_turn` when they run long. That is
        not an error and not a finished answer, so the turn is resumed by
        appending the paused assistant content and asking again.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        collected: list[Any] = []
        final: Any = None

        async with self._gate:
            for _ in range(max_resumes + 1):
                kwargs, use_beta = self._build(
                    model=model,
                    system=[{"type": "text", "text": system}],
                    messages=messages,
                    max_tokens=max_tokens,
                    effort=effort,
                    schema=None,
                    tools=tools,
                    want_thinking=on_thinking is not None,
                )
                try:
                    _, final = await self._stream_once(kwargs, use_beta, on_thinking)
                except RefusalError:
                    raise
                except (TypeError, anthropic.BadRequestError) as exc:
                    feature = _blamed_feature(getattr(exc, "message", "") or str(exc), self.caps)
                    if feature:
                        self.caps.disable(feature, "rejected for this request")
                        continue
                    raise
                except (
                    anthropic.RateLimitError,
                    anthropic.APIConnectionError,
                    anthropic.APITimeoutError,
                ) as exc:
                    log.warning("server-tool call transport error: %s", exc)
                    await asyncio.sleep(4.0)
                    continue

                await self._record_usage(model, getattr(final, "usage", None))
                collected.extend(final.content)

                if getattr(final, "stop_reason", None) != "pause_turn":
                    break
                messages.append({"role": "assistant", "content": final.content})

        if final is not None:
            # Hand back every block from every leg of the turn, so a resumed
            # search does not lose the results found before the pause.
            final.content = collected
        return final

    async def close(self) -> None:
        try:
            await self.client.close()
        except Exception:  # pragma: no cover - best effort
            pass


async def gather_capped(
    coros: list[Awaitable[Any]],
    *,
    on_done: Callable[[int, int], None] | None = None,
) -> list[Any]:
    """`asyncio.gather` that reports progress and never raises.

    Failures come back as exception objects in the result list, so one bad
    abstract cannot take down a 60-paper run.
    """
    total = len(coros)
    results: list[Any] = [None] * total
    done = 0

    async def run(index: int, coro: Awaitable[Any]) -> None:
        nonlocal done
        try:
            results[index] = await coro
        except Exception as exc:  # noqa: BLE001 - deliberately collected
            results[index] = exc
        finally:
            done += 1
            if on_done is not None:
                on_done(done, total)

    await asyncio.gather(*(run(i, c) for i, c in enumerate(coros)))
    return results
