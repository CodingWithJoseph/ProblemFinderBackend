from base64 import b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from app.browse_contract import BrowseCategory, BrowseSort


class BrowseCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    pipeline_version: str
    category: BrowseCategory
    sort: BrowseSort
    cluster_id: int = Field(gt=0)
    post_count: int = Field(ge=1)
    last_observed: AwareDatetime | None


def encode_browse_cursor(cursor: BrowseCursor) -> str:
    data = cursor.model_dump_json().encode("utf-8")
    return urlsafe_b64encode(data).decode("ascii").rstrip("=")


def decode_browse_cursor(
    token: str,
    *,
    category: BrowseCategory,
    sort: BrowseSort,
    pipeline_version: str,
) -> BrowseCursor:
    if not token or len(token) > 2048:
        raise ValueError("Invalid browse cursor")

    try:
        padding = "=" * (-len(token) % 4)
        data = b64decode(
            token + padding,
            altchars=b"-_",
            validate=True,
        )
        cursor = BrowseCursor.model_validate_json(data)
    except (Base64Error, ValueError, ValidationError) as exc:
        raise ValueError("Invalid browse cursor") from exc

    if (
        cursor.category != category
        or cursor.sort != sort
        or cursor.pipeline_version != pipeline_version
    ):
        raise ValueError("Cursor does not match this browse request")

    return cursor