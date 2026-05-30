"""Anthropic (Claude) client for opportunity "recommended next step" generation.

Centralised like openai_client. Defensive by design: if no API key is configured
generate_next_step returns None, so the feature degrades gracefully instead of
breaking the request. The static system prompt is marked for prompt caching.
"""
from anthropic import AsyncAnthropic

from app.config import get_settings

_settings = get_settings()

_client: AsyncAnthropic | None = (
    AsyncAnthropic(api_key=_settings.anthropic_api_key) if _settings.anthropic_api_key else None
)

_SYSTEM = (
    "You are an experienced startup advisor inside a market-research tool. Given a "
    "market opportunity — its defined problems, signal strength, and cluster context "
    "— recommend the single most valuable next step the founder should take. Be "
    "concrete and specific to the supplied data. Respond with one or two sentences in "
    "the imperative voice, with no preamble and without restating the inputs."
)


async def generate_next_step(context: str) -> str | None:
    """Return a concise recommended next step for the given opportunity context,
    or None if Anthropic is not configured or the call fails."""
    if _client is None:
        return None
    try:
        message = await _client.messages.create(
            model=_settings.anthropic_model,
            max_tokens=200,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": context}],
        )
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        ).strip()
        return text or None
    except Exception as exc:  # never let an advisory feature break the request
        print(f"anthropic generate_next_step failed: {exc}")
        return None
