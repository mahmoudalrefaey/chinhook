"""The one place the workflow talks to Azure OpenAI.

config.create_azure_client already builds the client and holds the two deployments, so this
module reuses it rather than introducing a provider of its own. What it adds is:

* token accounting, read from the provider's own usage object rather than estimated
* one entry point that always returns (text, TokenUsage), so no node can forget to record
  a call
* a JSON helper for the structured nodes, with a deterministic repair step when the model
  returns something that is not JSON

Nothing here decides anything about the workflow. The nodes do.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import config
from agent.state import TokenUsage

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


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


def chat(
    model: str,
    messages: list[dict[str, str]],
    tools: Optional[list[dict]] = None,
    max_completion_tokens: Optional[int] = None,
) -> tuple[Any, TokenUsage]:
    """One chat completion. Returns (message, TokenUsage).

    The assistant message is returned rather than just its text because the SQL generator
    still uses the repository's existing tool calling, and a tool call lives on the message
    object. temperature=0 everywhere, matching what the original pipeline used.
    """
    client = config.create_azure_client(model)
    deployment = config.get_model_config(model)["deployment"]
    kwargs: dict[str, Any] = {"model": deployment, "messages": messages, "temperature": 0}
    if tools:
        kwargs["tools"] = tools
    if max_completion_tokens:
        kwargs["max_completion_tokens"] = max_completion_tokens
    response = client.chat.completions.create(**kwargs)
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

    # One repair round trip. A structured reply that is not JSON is the most common
    # recoverable failure, and re-asking with the offending text is cheaper than abandoning
    # the turn. The repair is bounded, so a model that cannot produce JSON does not keep
    # the request alive: the caller falls back rather than waits.
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


def grounding_model(model: str) -> str:
    """The cheaper deployment used for the small structured jobs.

    Table evidence generation already uses the nano deployment for the same kind of short
    well-defined task, so this follows that existing choice instead of inventing a policy.
    """
    if "gpt-4.1-nano" in config.MODEL_CONFIGS and model != "gpt-4.1-nano":
        return "gpt-4.1-nano"
    return model
