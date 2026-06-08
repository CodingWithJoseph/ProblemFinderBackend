from datetime import datetime, timezone
from typing import Any, TypedDict

from app.models import Cluster


class SignalOverviewFields(TypedDict):
    signalOverview: dict[str, Any] | None
    overviewStatus: str | None
    overviewError: str | None
    overviewSchemaVersion: str | None
    overviewGeneratedAt: str | None
    overviewModelVersion: str | None
    overviewInputFingerprint: str | None


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def serialize_signal_overview(cluster: Cluster | None) -> SignalOverviewFields:
    """Expose the AI-generated overview as-is, with stable API metadata fields."""
    return {
        "signalOverview": cluster.signal_overview if cluster else None,
        "overviewStatus": cluster.overview_status if cluster else None,
        "overviewError": cluster.overview_error if cluster else None,
        "overviewSchemaVersion": cluster.overview_schema_version if cluster else None,
        "overviewGeneratedAt": _iso_or_none(cluster.overview_generated_at) if cluster else None,
        "overviewModelVersion": cluster.overview_model_version if cluster else None,
        "overviewInputFingerprint": cluster.overview_input_fingerprint if cluster else None,
    }
