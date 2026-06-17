from conftest import FakeResult, make_pipeline, make_team


async def test_pipeline_list_includes_project_team_and_issue_counts(client, fake_db, auth_headers):
    team = make_team(name="Validation Team")
    card = make_pipeline(team_id=team.id)
    fake_db.stub(execute=[
        FakeResult(rows=[card]),  # active cards
        FakeResult(rows=[team]),  # assigned team summaries
        FakeResult(rows=[
            (card.id, "issue", 2),
            (card.id, "kill_criteria", 1),
        ]),
    ])

    resp = await client.get("/pipeline", headers=auth_headers)

    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["teamId"] == team.id
    assert row["team"]["name"] == "Validation Team"
    assert row["openIssueCount"] == 2
    assert row["openKillCriteriaCount"] == 1
