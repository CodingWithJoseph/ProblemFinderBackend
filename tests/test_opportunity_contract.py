import pytest
from pydantic import ValidationError
from app.browse_contract import DiscoveryClusterResponse
from app.discovery_contract import DiscoveryCardResponse


@pytest.mark.parametrize("contract", [DiscoveryClusterResponse, DiscoveryCardResponse])
@pytest.mark.parametrize("field,value", [
    ("id", 0), ("postCount", -1), ("name", None), ("name", " "),
    ("summary", "\t\n"), ("problemStatement", ""), ("category", "unknown"),
])
def test_browse_and_discovery_reject_incomplete_opportunities(contract, field, value):
    payload = dict(id=1, category="uncategorized", name="Name", summary="Summary",
                   problemStatement="Problem", postCount=0, isWatched=False, date=None)
    payload[field] = value
    with pytest.raises(ValidationError):
        contract(**payload)
