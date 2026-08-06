from conftest import FakeResult, make_pipeline

from app.models import IdeAccessToken
from app.routes.ide_workspace import hash_access_token, mint_access_token


def test_minted_ide_token_is_prefixed_and_only_persisted_as_a_hash():
    cleartext, digest, prefix = mint_access_token()

    assert cleartext.startswith("imm_mcp_")
    assert prefix == cleartext[:16]
    assert digest == hash_access_token(cleartext)
    assert cleartext not in digest
    assert len(digest) == 64


async def test_create_ide_token_is_project_scoped_and_returns_secret_once(
    client,
    fake_db,
    auth_headers,
):
    card = make_pipeline()
    fake_db.stub(execute=[FakeResult(rows=[card])])

    response = await client.post(
        f"/pipeline/{card.id}/ide/access-tokens",
        headers=auth_headers,
        json={"name": "Cursor"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "Cursor"
    assert payload["token"].startswith("imm_mcp_")
    stored = next(row for row in fake_db.added if isinstance(row, IdeAccessToken))
    assert stored.pipeline_id == card.id
    assert stored.token_hash == hash_access_token(payload["token"])
    assert payload["token"] != stored.token_hash
    assert fake_db.commit_count == 1
