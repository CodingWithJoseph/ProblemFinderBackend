from datetime import datetime
from enum import StrEnum
from typing import List

from pydantic import BaseModel, ConfigDict, Field


class DiscoveryCategory(StrEnum):
    BUILD_AND_TOOLING = "build_and_tooling"
    DATA_AND_APIS = "data_and_apis"
    WORKFLOW_AND_AUTOMATION = "workflow_and_automation"
    UNCATEGORIZED = "uncategorized"


class DiscoveryCardResponse(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: int = Field(gt=0)
    category: DiscoveryCategory

    name: str = Field(min_length=1)
    problemStatement: str = Field(min_length=1)
    summary: str = Field(min_length=1)

    postCount: int = Field(ge=0)
    isWatched: bool
    date: datetime | None

class DiscoveryCollectionsResponse(BaseModel):
    category: DiscoveryCategory
    clusters: List[DiscoveryCardResponse]


class DiscoveryDashboardResponse(BaseModel):
    featured: DiscoveryCardResponse | None
    collections: List[DiscoveryCollectionsResponse]
