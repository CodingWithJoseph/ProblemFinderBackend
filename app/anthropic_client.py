"""Anthropic (Claude) client for opportunity "recommended next step" generation.

Centralised like openai_client. Defensive by design: if no API key is configured
generate_next_step returns None, so the feature degrades gracefully instead of
breaking the request. The static system prompt is marked for prompt caching.
"""
from app.config import get_settings

_settings = get_settings()

# Lazily created so the app imports even when the anthropic SDK isn't installed,
# and so a missing API key simply disables the feature.
_client = None
_init_attempted = False


def _get_client():
    global _client, _init_attempted
    if not _init_attempted:
        _init_attempted = True
        if _settings.anthropic_api_key:
            try:
                from anthropic import AsyncAnthropic
                _client = AsyncAnthropic(api_key=_settings.anthropic_api_key)
            except Exception as exc:  # SDK missing or misconfigured
                print(f"anthropic client init failed: {exc}")
                _client = None
    return _client


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
    client = _get_client()
    if client is None:
        return None
    try:
        message = await client.messages.create(
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


def brief_model_version() -> str:
    """The model string briefs are stamped with (for brief_model_version)."""
    return _settings.anthropic_model


async def generate_opportunity_brief(context: str) -> dict | None:
    """Generate the qualitative OpportunityBrief content via Claude.

    Returns the parsed content dict (see brief.parse_brief_content) or None when
    Anthropic is not configured, the call fails, or the output is unparseable.
    The static schema/system prompt is cached; the per-cluster context is volatile.
    """
    from app.brief import SYSTEM_PROMPT, parse_brief_content

    client = _get_client()
    if client is None:
        return None
    try:
        message = await client.messages.create(
            model=_settings.anthropic_model,
            max_tokens=4096,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": context}],
        )
        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        ).strip()
        return parse_brief_content(text)
    except Exception as exc:  # advisory feature — never break the request
        print(f"anthropic generate_opportunity_brief failed: {exc}")
        return None
