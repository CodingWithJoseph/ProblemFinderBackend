"""Server-side prompts + response models for the four staged opportunity briefs.

The brief pipeline has four stages, each with its own cluster column, API route
and prompt:

    discovery  → Signal page            — "is this worth investigating?"   (implemented)
    market     → Research / Market page  — audience, market size, competitors (coming soon)
    validation → Validate page           — validation questions, experiments  (coming soon)
    build      → Building / Report page   — full product / build spec          (legacy brief)

Only the *discovery* brief is generated today. The other three are scaffolded —
their columns and routes exist and return a ``{"status": "coming_soon"}`` stub
until generation is wired. The build stage is the migrated legacy founder brief;
its prompt + assembly live in ``app/brief.py`` and it is generated on demand via
the existing ``/clusters/{id}/brief/refresh`` endpoint.

Every brief shares the same lifecycle: check cache → generate if null or stale →
store → return, with a 7-day TTL and a ``?refresh=true`` override.

System prompts live here, server-side only, and are NEVER returned to the client.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field

# Cache lifetime shared by every staged brief.
BRIEF_TTL = timedelta(days=7)


# ── Discovery brief ─────────────────────────────────────────────────────────

DISCOVERY_SYSTEM_PROMPT = (
    "You are an expert startup analyst helping indie founders identify and "
    "evaluate business opportunities from real internet conversations. You "
    "analyze clusters of Reddit posts to produce structured opportunity briefs. "
    "Always respond with valid JSON only — no preamble, no markdown, no "
    "explanation."
)


class DiscoveryOpportunitySummary(BaseModel):
    title: str
    oneLineSummary: str
    problemSummary: str


class DiscoveryAudience(BaseModel):
    primaryUser: str
    secondaryUsers: list[str] = Field(default_factory=list)
    buyerType: str
    estimatedMarketSize: str


class DiscoveryBrief(BaseModel):
    """The discovery-brief response model (and cached document shape).

    The qualitative fields mirror the JSON Claude is asked to return. ``cached``
    and ``generated_at`` are server-managed: ``generated_at`` is stored inside
    the cached document (drives staleness) and ``cached`` flags whether a given
    response was served from cache.
    """

    opportunitySummary: DiscoveryOpportunitySummary
    audience: DiscoveryAudience
    productAngles: list[str]
    risks: list[str]
    validationQuestions: list[str]
    recommendedNextStep: str
    generated_at: datetime
    cached: bool = False


def _format_terms(top_tfidf_terms, limit: int = 5) -> str:
    """Top-N TF-IDF terms (``[{term, score}]``) as a bulleted list."""
    lines: list[str] = []
    for entry in (top_tfidf_terms or [])[:limit]:
        if isinstance(entry, dict):
            term = str(entry.get("term", "")).strip()
        else:
            term = str(entry).strip()
        if term:
            lines.append(f"- {term}")
    return "\n".join(lines) or "- (no signal terms available)"


def _format_sources(source_breakdown) -> str:
    """Source breakdown (``[{platform, site, count}]``) as ``r/sub: N posts``."""
    lines: list[str] = []
    for entry in source_breakdown or []:
        if not isinstance(entry, dict):
            continue
        site = str(entry.get("site") or entry.get("platform") or "").strip()
        if not site:
            continue
        count = entry.get("count")
        label = site if site.lower().startswith("r/") else f"r/{site}"
        lines.append(f"{label}: {count if count is not None else '?'} posts")
    return "\n".join(lines) or "(no source breakdown available)"


def _format_posts(posts) -> str:
    """Representative posts as ``Title: … | Excerpt: … | Upvotes: …`` lines."""
    lines: list[str] = []
    for p in posts:
        title = (getattr(p, "title", None) or "").strip()
        excerpt = (getattr(p, "description", None) or "").strip()[:300]
        score = getattr(p, "source_score", None)
        upvotes = int(score) if isinstance(score, (int, float)) else "?"
        lines.append(f"Title: {title} | Excerpt: {excerpt} | Upvotes: {upvotes}")
    return "\n".join(lines) or "(no representative posts available)"


def build_discovery_user_prompt(cluster, posts) -> str:
    """Assemble the per-cluster discovery user prompt from real cluster data."""
    return (
        "Analyze this opportunity cluster and produce a structured brief.\n\n"
        f"Cluster: {cluster.name or 'Unnamed cluster'}\n"
        f"Type: {cluster.opportunity_type or 'Unknown'}\n"
        f"Domain: {cluster.opportunity_domain or 'Unknown'}\n"
        f"Summary: {cluster.summary or 'No summary available.'}\n\n"
        "Top signals from posts:\n"
        f"{_format_terms(cluster.top_tfidf_terms)}\n\n"
        "Where people are talking:\n"
        f"{_format_sources(cluster.source_breakdown)}\n\n"
        "Representative posts:\n"
        f"{_format_posts(posts)}\n\n"
        "Return ONLY this JSON structure, nothing else:\n"
        "{\n"
        '  "opportunitySummary": {\n'
        '    "title": "string — 5-8 word product/opportunity title",\n'
        '    "oneLineSummary": "string — one sentence value prop",\n'
        '    "problemSummary": "string — 2-3 sentences describing the core problem"\n'
        "  },\n"
        '  "audience": {\n'
        '    "primaryUser": "string",\n'
        '    "secondaryUsers": ["string"],\n'
        '    "buyerType": "string — B2B/B2C/B2B2C",\n'
        '    "estimatedMarketSize": "string — rough estimate with reasoning"\n'
        "  },\n"
        '  "productAngles": ["string — 2-3 specific product directions"],\n'
        '  "risks": ["string — 2-3 specific risks"],\n'
        '  "validationQuestions": ["string — 3 questions to validate before building"],\n'
        '  "recommendedNextStep": "string — one concrete next action"\n'
        "}"
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_object(text: str) -> dict | None:
    """Best-effort extraction of a single JSON object from a model response.

    Tolerates code fences / stray prose around the object. Returns None when no
    parseable object is found — the caller maps that to a 503 and never caches.
    """
    if not text:
        return None
    match = _JSON_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def is_fresh(generated_at: datetime | None) -> bool:
    """True when a cached brief is within the 7-day TTL."""
    if generated_at is None:
        return False
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - generated_at < BRIEF_TTL


# ── Market / Validation prompts (scaffolded; land when generation is wired) ──

MARKET_SYSTEM_PROMPT = (
    "You are an expert market analyst evaluating a business opportunity for an "
    "indie founder. (Market brief generation is not yet implemented — this "
    "prompt is scaffolding.)"
)

VALIDATION_SYSTEM_PROMPT = (
    "You are an expert product-validation coach helping an indie founder design "
    "cheap experiments to de-risk an opportunity. (Validation brief generation "
    "is not yet implemented — this prompt is scaffolding.)"
)

# The build (legacy founder) brief prompt and assembly live in app/brief.py
# (SYSTEM_PROMPT / assemble_brief) and are generated via /brief/refresh.
