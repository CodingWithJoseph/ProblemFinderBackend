"""Validated contracts for lightweight conversational Search persistence."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.search_contract import ClusterSearchQuery, SearchInterpretationResponse


class SearchSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()


class SearchSessionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=160)
    saved: bool | None = None
    archived: bool | None = None


class SearchTurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_message: str = Field(min_length=2, max_length=2_000)
    interpretation: SearchInterpretationResponse


class SearchRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: ClusterSearchQuery
    result_cluster_ids: list[str] = Field(default_factory=list, max_length=50)
    result_count: int = Field(ge=0)

    @field_validator("result_cluster_ids")
    @classmethod
    def normalize_result_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = str(raw_value).strip()
            if not value or len(value) > 128:
                continue
            if value not in seen:
                seen.add(value)
                normalized.append(value)
        return normalized


class SearchTurnResponse(BaseModel):
    id: str
    user_message: str
    interpretation: SearchInterpretationResponse
    created_at: datetime


class SearchRunResponse(BaseModel):
    id: str
    draft: ClusterSearchQuery
    result_cluster_ids: list[str]
    result_count: int
    created_at: datetime


class SearchSessionSummary(BaseModel):
    id: str
    title: str
    saved: bool
    archived: bool
    expires_at: datetime | None
    last_activity_at: datetime
    created_at: datetime
    updated_at: datetime


class SearchSessionDetail(SearchSessionSummary):
    turns: list[SearchTurnResponse]
    runs: list[SearchRunResponse]


SearchSessionView = Literal["recent", "saved", "archived"]
