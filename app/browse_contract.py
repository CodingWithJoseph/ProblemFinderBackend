from datetime import datetime
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel
from app.search_contract import ClusterSearchQuery


class DiscoveryClusterResponse(BaseModel):
    id: int
    name: str | None
    summary: str | None
    problemStatement: str | None
    postCount: int
    isWatched: bool
    date: datetime | None

class BrowsePageResponse(BaseModel):
    clusters: list[DiscoveryClusterResponse]
    nextCursor: str | None

class BrowseCategory(StrEnum):
    ALL = "all"


class BrowseSort(StrEnum):
    NEWEST = "newest"
    OLDEST = "oldest"
    LARGEST = "largest"
    SMALLEST = "smallest"


CATEGORY_QUERIES: dict[BrowseCategory, str | None] = {
    BrowseCategory.ALL: None,
}

SORT_TYPE: dict[BrowseSort, Literal] = {
    BrowseSort.NEWEST: 'newest',
    BrowseSort.OLDEST: 'oldest',
    BrowseSort.LARGEST: 'largest',
    BrowseSort.SMALLEST: 'smallest',
}


def build_browse_filters(category: BrowseCategory, sort: BrowseSort) -> ClusterSearchQuery:
    return ClusterSearchQuery(
        query=CATEGORY_QUERIES[category],
        sort=SORT_TYPE[sort],
    )
