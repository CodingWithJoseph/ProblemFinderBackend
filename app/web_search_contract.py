"""Strict public contract for post-confirmation external evidence search."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


WEB_SEARCH_MAX_RESULTS = 5


class WebSearchRequest(BaseModel):
    """A user-confirmed external search; never accepted implicitly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=2, max_length=500)
    confirmed: Literal[True]
    max_results: int = Field(default=WEB_SEARCH_MAX_RESULTS, ge=1, le=WEB_SEARCH_MAX_RESULTS)


class WebSearchSource(BaseModel):
    """Sanitized citation metadata returned from the provider's search tool."""

    model_config = ConfigDict(extra="forbid")

    citation_id: str = Field(pattern=r"^web-[1-5]$")
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2_000)
    snippet: str = Field(max_length=1_000)
    score: float | None = Field(default=None, ge=0, le=1)


class WebSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    sources: list[WebSearchSource] = Field(max_length=WEB_SEARCH_MAX_RESULTS)
    provider: Literal["groq_web_search"] = "groq_web_search"
