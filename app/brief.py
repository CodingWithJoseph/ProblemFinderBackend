"""Opportunity Brief — shared prompt, parsing, and assembly.

A brief is a founder-ready summary of a high-signal cluster, conforming to the
OpportunityBrief TypeScript type the web app renders. Claude generates only the
*qualitative* fields; the server fills *quantitative* fields (signal-strength
numbers, evidence counts, date range, representative posts) and metadata
(ids/timestamps) from real data, so numbers are never hallucinated.

This module is the single source of truth for the prompt + JSON handling, shared
by the on-demand refresh endpoint and (mirrored) by the AI batch pipeline.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

_JSON_RE = re.compile(r"\{.*}", re.DOTALL)

_BUYER_TYPES = {"consumer", "b2b", "prosumer", "unknown"}
_MARKET_SIZES = {"niche", "mid", "mass"}
_DIFFICULTIES = {"low", "medium", "high"}
_MOMENTUM = {"emerging", "growing", "steady", "fading"}

# Static system prompt — cached (prompt caching) since it never varies. The
# volatile per-cluster context goes in the user message, after the cache point.
SYSTEM_PROMPT = """You are a startup analyst generating a founder-ready "opportunity brief" from clustered market-signal data: real social-media posts (mostly Reddit) where people express a shared problem.

You receive structured cluster data — the cluster name, aggregate signal metrics, willingness-to-pay and workaround rates, and the top representative posts.

Return ONLY a single JSON object — no markdown, no code fences, no commentary before or after it. The object MUST have EXACTLY these keys and value types:

{
  "title": string,                  // specific opportunity title, <= 8 words
  "oneLineSummary": string,         // one sentence capturing the opportunity
  "problemSummary": string,         // 2-4 sentences on the underlying problem
  "audience": {
    "primaryUser": string,
    "secondaryUsers": string[],     // [] if none
    "buyerType": "consumer" | "b2b" | "prosumer" | "unknown",
    "estimatedMarketSize": "niche" | "mid" | "mass"
  },
  "topSignals": string[],           // 3-6 concrete evidence bullets from the data
  "workarounds": string[],          // how people cope today (hacks, manual steps)
  "currentAlternatives": string[],  // existing products/services they use
  "productAngles": [                // 2-4 distinct product directions
    { "title": string, "description": string, "mvpDifficulty": "low" | "medium" | "high", "targetUser": string }
  ],
  "risks": string[],                // 2-4 key risks
  "validationQuestions": string[],  // 3-5 questions the founder should answer next
  "recommendedNextStep": string,    // the single most valuable next action
  "momentum": "emerging" | "growing" | "steady" | "fading"
}

Be concrete and specific to the supplied data. Do not invent statistics or quote numbers that are not provided. Keep every string concise."""

# Keys Claude is responsible for (everything else is server-filled).
_CONTENT_DEFAULTS = {
    "title": "Untitled opportunity",
    "oneLineSummary": "",
    "problemSummary": "",
    "topSignals": [],
    "workarounds": [],
    "currentAlternatives": [],
    "productAngles": [],
    "risks": [],
    "validationQuestions": [],
    "recommendedNextStep": "",
    "momentum": "emerging",
}


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _enum(value, allowed: set[str], default: str) -> str:
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def _coerce_audience(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    audience = {
        "primaryUser": str(raw.get("primaryUser") or "Unknown").strip() or "Unknown",
        "buyerType": _enum(raw.get("buyerType"), _BUYER_TYPES, "unknown"),
        "estimatedMarketSize": _enum(raw.get("estimatedMarketSize"), _MARKET_SIZES, "niche"),
    }
    secondary = _as_str_list(raw.get("secondaryUsers"))
    if secondary:
        audience["secondaryUsers"] = secondary
    return audience


def _coerce_angles(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    angles = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "").strip()
        desc = str(a.get("description") or "").strip()
        if not title and not desc:
            continue
        angle = {"title": title or "Untitled angle", "description": desc}
        if a.get("mvpDifficulty"):
            angle["mvpDifficulty"] = _enum(a.get("mvpDifficulty"), _DIFFICULTIES, "medium")
        target = str(a.get("targetUser") or "").strip()
        if target:
            angle["targetUser"] = target
        angles.append(angle)
    return angles


def parse_brief_content(text: str) -> dict | None:
    """Extract + coerce the model's JSON into the qualitative content fields.

    Tolerant of code fences / stray prose. Returns None only if no JSON object
    can be found; otherwise always returns a fully-populated content dict with
    safe defaults, so a partially-valid response still yields a usable brief.
    """
    match = _JSON_RE.search(text or "")
    if not match:
        return None
    try:
        raw = json.loads(match.group(0))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None

    content = {
        "title": str(raw.get("title") or _CONTENT_DEFAULTS["title"]).strip(),
        "oneLineSummary": str(raw.get("oneLineSummary") or "").strip(),
        "problemSummary": str(raw.get("problemSummary") or "").strip(),
        "audience": _coerce_audience(raw.get("audience")),
        "topSignals": _as_str_list(raw.get("topSignals")),
        "workarounds": _as_str_list(raw.get("workarounds")),
        "currentAlternatives": _as_str_list(raw.get("currentAlternatives")),
        "productAngles": _coerce_angles(raw.get("productAngles")),
        "risks": _as_str_list(raw.get("risks")),
        "validationQuestions": _as_str_list(raw.get("validationQuestions")),
        "recommendedNextStep": str(raw.get("recommendedNextStep") or "").strip(),
        "momentum": _enum(raw.get("momentum"), _MOMENTUM, "emerging"),
    }
    return content


def momentum_from_growth(growth_acceleration: float | None, growth_rate_wow: float | None) -> str:
    """Derive momentum from snapshot growth signals (server-side fallback/override)."""
    accel = growth_acceleration if growth_acceleration is not None else 0.0
    wow = growth_rate_wow if growth_rate_wow is not None else 0.0
    if accel > 0.15 or wow > 0.5:
        return "growing"
    if accel < -0.15 or wow < -0.2:
        return "fading"
    if wow > 0.1:
        return "emerging"
    return "steady"


def assemble_brief(content: dict, *, meta: dict, evidence: dict, signal_strength: dict) -> dict:
    """Merge Claude's qualitative content with server metadata + real numbers."""
    brief = {
        "id": meta["id"],
        "clusterId": meta["clusterId"],
        "snapshotId": meta.get("snapshotId", ""),
        "generatedAt": meta["generatedAt"],
        "generatedBy": meta["generatedBy"],
        "title": content["title"],
        "oneLineSummary": content["oneLineSummary"],
        "problemSummary": content["problemSummary"],
        "audience": content["audience"],
        "evidence": {
            "topSignals": content["topSignals"],
            "representativePosts": evidence.get("representativePosts", []),
            "totalPostsAnalyzed": evidence.get("totalPostsAnalyzed", 0),
            "dateRange": evidence.get("dateRange", {"from": "", "to": ""}),
        },
        "signalStrength": signal_strength,
        "workarounds": content["workarounds"],
        "currentAlternatives": content["currentAlternatives"],
        "productAngles": content["productAngles"],
        "risks": content["risks"],
        "validationQuestions": content["validationQuestions"],
        "recommendedNextStep": content["recommendedNextStep"],
        "momentum": content["momentum"],
    }
    if meta.get("refreshedAt"):
        brief["refreshedAt"] = meta["refreshedAt"]
    return brief


def new_meta(cluster_id, snapshot_id, generated_by: str, *, refreshed: bool = False) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "id": str(uuid.uuid4()),
        "clusterId": str(cluster_id),
        "snapshotId": str(snapshot_id) if snapshot_id is not None else "",
        "generatedAt": now,
        "generatedBy": generated_by,
    }
    if refreshed:
        meta["refreshedAt"] = now
    return meta
