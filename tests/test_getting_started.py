from app.services import getting_started as gs


async def test_getting_started_marks_next_incomplete_step(fake_db):
    # scalar() order matches _step_completion: save_pipeline, discover_problem,
    # create_task, launch_product, connect_data. First two done, rest not.
    fake_db.stub(scalar=["pipe-1", "prob-1", None, None, None])
    payload = await gs.getting_started_payload("uid-1", fake_db)

    assert [s["key"] for s in payload["steps"]] == [
        "save_pipeline", "discover_problem", "create_task", "launch_product", "connect_data",
    ]
    assert [s["done"] for s in payload["steps"]] == [True, True, False, False, False]
    assert payload["completedCount"] == 2
    assert payload["totalCount"] == 5
    assert payload["complete"] is False
    # "Do this next" is the first incomplete step in funnel order.
    assert payload["nextStep"]["key"] == "create_task"
    assert payload["nextStep"]["routeKey"] == "tasks"


async def test_getting_started_next_step_skips_out_of_order_completion(fake_db):
    # A later step done but an earlier one not → next is still the earliest gap.
    fake_db.stub(scalar=[None, None, None, "pipe-1", "src-1"])
    payload = await gs.getting_started_payload("uid-1", fake_db)
    assert payload["completedCount"] == 2
    assert payload["nextStep"]["key"] == "save_pipeline"


async def test_getting_started_complete_when_all_done(fake_db):
    fake_db.stub(scalar=["a", "b", "c", "d", "e"])
    payload = await gs.getting_started_payload("uid-1", fake_db)
    assert payload["completedCount"] == 5
    assert payload["complete"] is True
    assert payload["nextStep"] is None


async def test_getting_started_all_incomplete_starts_at_first(fake_db):
    fake_db.stub(scalar=[None, None, None, None, None])
    payload = await gs.getting_started_payload("uid-1", fake_db)
    assert payload["completedCount"] == 0
    assert payload["nextStep"]["key"] == "save_pipeline"
    assert payload["nextStep"]["actionLabel"] == "Open the pipeline"
