import pytest
from scripts.patch_discovery_preview_categories import CategoryStore, plan_patch, validate_assignments


def record(identifier=1, category="uncategorized", **kwargs):
    return dict(id=identifier, discovery_category=category, name="Name", summary="Summary", pipeline_version="v2", **kwargs)


def test_patch_only_plans_uncategorized_memberships_and_is_repeatable():
    assignments = validate_assignments([dict(id=1, category="build_and_tooling"), dict(id=2, category="data_and_apis")])
    changes = plan_patch(assignments, [record(), record(2, "data_and_apis")])
    assert changes == [dict(id=1, previous="uncategorized", category="build_and_tooling")]
    assert plan_patch(assignments, [record(1, "build_and_tooling"), record(2, "data_and_apis")]) == []


@pytest.mark.parametrize("rows", [[], [dict(id=0, category="data_and_apis")], [dict(id=1, category="unknown")], [dict(id=1, category="data_and_apis"), dict(id=1, category="build_and_tooling")]])
def test_invalid_or_conflicting_assignments_fail_before_writing(rows):
    with pytest.raises(ValueError):
        validate_assignments(rows)


def test_existing_classification_and_missing_rows_are_preserved():
    for records in [[], [record(category="workflow_and_automation")]]:
        with pytest.raises(ValueError):
            plan_patch({1: "data_and_apis"}, records)


def test_update_and_restore_use_compare_and_set_filters():
    class FakeStore(CategoryStore):
        def __init__(self): self.calls = []
        def request(self, method, query, body=None):
            self.calls.append((method, query, body))
            return [dict(id=1)]
    store = FakeStore()
    changes = [dict(id=1, previous="uncategorized", category="data_and_apis")]
    assert store.update(changes) == 1
    assert store.calls[-1][1]["discovery_category"] == "eq.uncategorized"
    assert store.calls[-1][2] == dict(discovery_category="data_and_apis")
    assert store.update(changes, restore=True) == 1
    assert store.calls[-1][1]["discovery_category"] == "eq.data_and_apis"
    assert store.calls[-1][2] == dict(discovery_category="uncategorized")
