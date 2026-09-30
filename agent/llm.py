"""The one place the workflow talks to a language model.

Any OpenAI-compatible chat endpoint works: OpenAI itself, Azure OpenAI's v1 endpoint,
OpenRouter, Groq, Together, Gemini's and Anthropic's OpenAI-compatible endpoints, a
self-hosted vLLM. Which one, and with which key and model, is whatever the current session
connected with (see runtime.py), never anything configured on the server.

What this adds on top of the client:

* token accounting, read from the provider's own usage object rather than estimated
* one entry point that always returns (text, TokenUsage), so no node can forget to record
  a call
* a JSON helper for the structured nodes, with a deterministic repair step when the model
  returns something that is not JSON
* tolerance for the ways "OpenAI-compatible" endpoints differ from each other (see _call)

Nothing here decides anything about the workflow. The nodes do.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from typing import Any, Callable, Optional

import openai
from openai import OpenAI

import runtime
from agent.state import TokenUsage

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# ---------- clients ----------

_clients: dict[tuple[str, str], OpenAI] = {}
_clients_lock = threading.Lock()


def _client(settings: runtime.LLMSettings) -> OpenAI:
    """The client for this endpoint and key, built once and reused.

    Keyed by the key as well as the URL: two sessions using the same provider with different
    keys must each be billed to their own, never to whichever key happened to arrive first.
    The key itself is hashed rather than held as a dictionary key in the clear.
    """
    cache_key = (settings.base_url, hashlib.sha256(settings.api_key.encode("utf-8")).hexdigest())
    client = _clients.get(cache_key)
    if client is None:
        with _clients_lock:
            client = _clients.get(cache_key)
            if client is None:
                client = OpenAI(
                    base_url=settings.base_url,
                    api_key=settings.api_key,
                    timeout=90,
                    max_retries=2,
                    # A public endpoint must not be able to bounce this server on to a
                    # private address with a redirect: the URL was checked, not where it
                    # might redirect to.
                    http_client=openai.DefaultHttpxClient(follow_redirects=False),
                )
                _clients[cache_key] = client
    return client


# ---------- endpoint quirks ----------
# Parameters some OpenAI-compatible endpoints reject outright: reasoning models refuse any
# temperature but the default, some servers only know max_tokens rather than
# max_completion_tokens, and some refuse stream_options. The first time an endpoint rejects
# one, the call is retried without it and that is remembered for the (endpoint, model) pair,
# so later calls do not pay for the same rejection again.
_quirks: dict[tuple[str, str], set[str]] = {}
_NO_TEMPERATURE = "no_temperature"
_MAX_TOKENS = "max_tokens"
_NO_STREAM_USAGE = "no_stream_usage"


def _quirk_for(error_text: str, active: set[str], kwargs: dict[str, Any]) -> Optional[str]:
    text = error_text.lower()
    if "temperature" in text and _NO_TEMPERATURE not in active and "temperature" in kwargs:
        return _NO_TEMPERATURE
    if "max_completion_tokens" in text and _MAX_TOKENS not in active:
        return _MAX_TOKENS
    if "stream_options" in text and _NO_STREAM_USAGE not in active and "stream_options" in kwargs:
        return _NO_STREAM_USAGE
    return None


def _apply(kwargs: dict[str, Any], active: set[str]) -> dict[str, Any]:
    call = dict(kwargs)
    if _NO_TEMPERATURE in active:
        call.pop("temperature", None)
    if _MAX_TOKENS in active and "max_completion_tokens" in call:
        call["max_tokens"] = call.pop("max_completion_tokens")
    if _NO_STREAM_USAGE in active:
        call.pop("stream_options", None)
    return call


def _call(settings: runtime.LLMSettings, **kwargs: Any):
    """chat.completions.create, retried without whatever parameter the endpoint refused."""
    key = (settings.base_url, kwargs["model"])
    active = _quirks.setdefault(key, set())
    for _attempt in range(4):
        try:
            return _client(settings).chat.completions.create(**_apply(kwargs, active))
        except openai.BadRequestError as exc:
            quirk = _quirk_for(str(exc), active, kwargs)
            if quirk is None:
                raise
            active.add(quirk)
    return _client(settings).chat.completions.create(**_apply(kwargs, active))


def _usage_from(response) -> TokenUsage:
    """Read provider token counts off a response, defaulting to zero when absent."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TokenUsage(llm_calls=1)
    return TokenUsage(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        llm_calls=1,
    )


# ---------- calls ----------

def chat(
    model: str,
    messages: list[dict[str, str]],
    tools: Optional[list[dict]] = None,
    max_completion_tokens: Optional[int] = None,
) -> tuple[Any, TokenUsage]:
    """One chat completion. Returns (message, TokenUsage).

    The assistant message is returned rather than just its text because the SQL generator
    uses tool calling, and a tool call lives on the message object. temperature=0 wherever
    the endpoint accepts it.
    """
    return _chat_with(runtime.current().llm, model, messages, tools, max_completion_tokens)


def _chat_with(
    settings: runtime.LLMSettings,
    model: str,
    messages: list[dict[str, str]],
    tools: Optional[list[dict]] = None,
    max_completion_tokens: Optional[int] = None,
) -> tuple[Any, TokenUsage]:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0}
    if tools:
        kwargs["tools"] = tools
    if max_completion_tokens:
        kwargs["max_completion_tokens"] = max_completion_tokens
    response = _call(settings, **kwargs)
    return response.choices[0].message, _usage_from(response)


def chat_text(
    model: str,
    system: str,
    user: str,
    max_completion_tokens: Optional[int] = None,
) -> tuple[str, TokenUsage]:
    """A single-turn text call. The most common shape in this workflow."""
    message, usage = chat(
        model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_completion_tokens=max_completion_tokens,
    )
    return (message.content or ""), usage


def chat_stream(
    model: str,
    system: str,
    user: str,
    on_token: Optional[Callable[[str], None]] = None,
    max_completion_tokens: Optional[int] = None,
) -> tuple[str, TokenUsage]:
    """A single-turn text call, with the reply pushed to on_token as it is written.

    Falls back to chat_text when no callback is given, so a caller that has nowhere to show a
    live reply is not paying for a streamed response it reads no faster than a finished one.
    """
    if on_token is None:
        return chat_text(model, system, user, max_completion_tokens=max_completion_tokens)

    settings = runtime.current().llm
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if max_completion_tokens:
        kwargs["max_completion_tokens"] = max_completion_tokens

    parts: list[str] = []
    usage = TokenUsage(llm_calls=1)
    for chunk in _call(settings, **kwargs):
        if getattr(chunk, "usage", None):
            usage.input_tokens = chunk.usage.prompt_tokens or 0
            usage.output_tokens = chunk.usage.completion_tokens or 0
        if not chunk.choices or chunk.choices[0].delta is None:
            # The usage-carrying chunk arrives this way on some endpoints: choices holding one
            # entry whose delta is None, rather than an empty choices list. Either shape means
            # there is no text in this chunk.
            continue
        delta = chunk.choices[0].delta.content
        if delta:
            parts.append(delta)
            on_token(delta)
    return "".join(parts), usage


def parse_json(text: str) -> Optional[Any]:
    """Pull a JSON object out of a model response.

    Tries a fenced block first, then the outermost brace pair. Returns None rather than
    raising, so the caller decides whether a bad response is a repair or a fallback.
    """
    if not text:
        return None
    candidate = text.strip()
    fence = _JSON_FENCE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()
    else:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        return None


def chat_json(
    model: str,
    system: str,
    user: str,
    max_completion_tokens: Optional[int] = 1400,
) -> tuple[Optional[dict], TokenUsage]:
    """A single-turn call that must come back as a JSON object.

    One repair round trip is allowed, because a malformed structured response is the most
    common recoverable failure and re-asking with the offending text is cheaper than
    abandoning the turn. If the repair also fails, the caller decides what to do; no
    fabricated structure is ever invented here.
    """
    text, usage = chat_text(model, system, user, max_completion_tokens=max_completion_tokens)
    payload = parse_json(text)
    if isinstance(payload, dict):
        return payload, usage

    try:
        repaired, repair_usage = chat_text(
            model,
            system,
            f"{user}\n\nYour previous reply was not valid JSON:\n{text[:600]}\n\n"
            "Reply with the JSON object only.",
            max_completion_tokens=max_completion_tokens,
        )
    except Exception:  # noqa: BLE001
        return None, usage
    usage.add(repair_usage)
    payload = parse_json(repaired)
    return (payload if isinstance(payload, dict) else None), usage


# ---------- setup ----------

class LLMSetupError(Exception):
    """The model endpoint cannot be used, with a reason a person can act on."""


_PING_TOOL = [{
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Acknowledge the request.",
        "parameters": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        },
    },
}]


def describe_error(exc: Exception) -> str:
    """What went wrong talking to the model, in words that say what to change."""
    if isinstance(exc, openai.AuthenticationError):
        return "The API key was rejected. Check that it is correct and belongs to this provider."
    if isinstance(exc, openai.PermissionDeniedError):
        return "The API key is not allowed to use this model."
    if isinstance(exc, openai.NotFoundError):
        return "The model was not found at this base URL. Check the model name and the base URL."
    if isinstance(exc, openai.RateLimitError):
        return "The provider refused the request for rate limit or quota reasons. Check your plan and try again."
    if isinstance(exc, openai.APITimeoutError):
        return "The model endpoint did not answer in time."
    if isinstance(exc, openai.APIConnectionError):
        return "Could not reach the base URL. Check that it is correct and publicly reachable."
    if isinstance(exc, openai.BadRequestError):
        return f"The endpoint rejected the request: {exc}"
    return f"{type(exc).__name__}: {exc}"


def probe(settings: runtime.LLMSettings) -> None:
    """Check that a model can do what the workflow needs: answer, and call a tool.

    Tool calling is not optional: every SQL query is written through one. A model that answers
    in prose instead would fail every question later, so it is refused here, up front, with
    the reason, rather than one question at a time. Both the model and the fast model, when
    one is given, are checked. Raises LLMSetupError; returns None when the endpoint is usable.
    """
    models = [settings.model] + ([settings.fast_model] if settings.fast_model else [])
    for model in dict.fromkeys(models):
        try:
            message, _usage = _chat_with(
                settings,
                model,
                [{"role": "user", "content": "Call the ping tool with ok set to true."}],
                tools=_PING_TOOL,
                max_completion_tokens=200,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMSetupError(f"{model}: {describe_error(exc)}") from exc
        if not getattr(message, "tool_calls", None):
            raise LLMSetupError(
                f"{model} answered but did not call a tool. This app writes every query "
                "through a tool call, so it needs a model that supports tool (function) "
                "calling."
            )


def list_models(settings: runtime.LLMSettings) -> list[str]:
    """The model ids the endpoint lists, or an empty list where it lists none."""
    try:
        return sorted(model.id for model in _client(settings).models.list())
    except Exception:  # noqa: BLE001
        return []
