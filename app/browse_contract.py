from enum import StrEnum
from typing import Literal
from pydantic import BaseModel
from app.discovery_contract import DiscoveryCardResponse
from app.search_contract import ClusterSearchQuery


class DiscoveryClusterResponse(DiscoveryCardResponse):
    """Browse and Discovery expose the same complete opportunity contract."""


class BrowsePageResponse(BaseModel):
    clusters: list[DiscoveryClusterResponse]
    nextCursor: str | None


class BrowseCategory(StrEnum):
    ALL = "all"
    BUILD_AND_TOOLING = "build_and_tooling"
    DATA_AND_APIS = "data_and_apis"
    WORKFLOW_AND_AUTOMATION = "workflow_and_automation"
    UNCATEGORIZED = "uncategorized"


class BrowseSort(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    LARGEST = "largest"
    SMALLEST = "smallest"

SORT_TYPE: dict[BrowseSort, Literal] = {
    BrowseSort.NEWEST: 'newest',
    BrowseSort.OLDEST: 'oldest',
    BrowseSort.LARGEST: 'largest',
    BrowseSort.SMALLEST: 'smallest',
}


def build_browse_filters(sort: BrowseSort) -> ClusterSearchQuery:
    return ClusterSearchQuery(sort=SORT_TYPE[sort],)
