import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException, Request

import app.auth as auth

UID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def signed_token():
    private = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="test-key", alg="ES256", use="sig")
    verifier = auth.SupabaseTokenVerifier()
    verifier._keys = {"test-key": jwt.PyJWK.from_dict(jwk)}
    verifier._expires_at = time.monotonic() + 300
    verifier._last_fetch = time.monotonic()

    def mint(**overrides):
        payload = {
            "iss": auth.settings.supabase_issuer, "aud": "authenticated", "sub": UID,
            "iat": int(time.time()), "exp": int(time.time()) + 300,
            "role": "authenticated", "amr": [{"method": "password", "timestamp": time.time()}],
        }
        payload.update(overrides)
        return jwt.encode(payload, private, algorithm="ES256", headers={"kid": "test-key"})

    return verifier, mint, private, jwk


async def test_verifies_supabase_signature_and_user_uuid(signed_token):
    verifier, mint, *_ = signed_token
    assert (await verifier.verify(mint()))["sub"] == UID


@pytest.mark.parametrize("overrides", [
    {"iss": "https://another-project.supabase.co/auth/v1"},
    {"aud": "other-api"}, {"exp": int(time.time()) - 60},
    {"nbf": int(time.time()) + 60}, {"sub": "legacy-user"},
    {"role": "service_role"}, {"is_anonymous": True},
    {"iat": int(time.time()) + 60}, {"exp": None},
])
async def test_rejects_invalid_claims(signed_token, overrides):
    verifier, mint, *_ = signed_token
    with pytest.raises(HTTPException) as error:
        await verifier.verify(mint(**overrides))
    assert error.value.status_code == 401


async def test_rejects_another_signing_key(signed_token):
    verifier, mint, *_ = signed_token
    payload = jwt.decode(mint(), options={"verify_signature": False})
    other = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(payload, other, algorithm="ES256", headers={"kid": "test-key"})
    with pytest.raises(HTTPException) as error:
        await verifier.verify(token)
    assert error.value.status_code == 401


async def test_rejects_unsigned_and_opaque_tokens(signed_token):
    verifier, *_ = signed_token
    for token in [jwt.encode({"sub": UID}, key="", algorithm="none"), "imm_oauth_access_test", "bad", "x" * 17000]:
        with pytest.raises(HTTPException) as error:
            await verifier.verify(token)
        assert error.value.status_code == 401


async def test_jwks_outage_is_503(monkeypatch, signed_token):
    verifier, mint, *_ = signed_token
    verifier._expires_at = 0
    async def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx.AsyncClient, "get", unavailable)
    with pytest.raises(HTTPException) as error:
        await verifier.verify(mint())
    assert error.value.status_code == 503


async def test_key_rotation_reloads_fixed_project_endpoint(monkeypatch, signed_token):
    verifier, mint, _, jwk = signed_token
    verifier._keys = {}; verifier._expires_at = 0
    calls = []
    async def keys(_client, url):
        calls.append(url)
        return httpx.Response(200, json={"keys": [jwk]}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx.AsyncClient, "get", keys)
    await verifier.verify(mint())
    await verifier.verify(mint())
    assert calls == [f"{auth.settings.supabase_issuer}/.well-known/jwks.json"]


async def test_refresh_does_not_count_as_recent_reauthentication(monkeypatch):
    request = Request({"type": "http", "headers": []})
    request.state.access_token = "token"
    with pytest.raises(HTTPException) as error:
        await auth.require_recent_user(request, {
            "sub": UID, "iat": time.time(),
            "amr": [{"method": "password", "timestamp": time.time() - 3600}],
        })
    assert error.value.status_code == 403


async def test_live_user_identity_must_match_recent_token(monkeypatch):
    async def other_user(_token):
        return {"id": "22222222-2222-4222-8222-222222222222"}
    monkeypatch.setattr(auth, "supabase_user", other_user)
    request = Request({"type": "http", "headers": []}); request.state.access_token = "token"
    with pytest.raises(HTTPException) as error:
        await auth.require_recent_user(request, {"sub": UID, "amr": [{"method": "password", "timestamp": time.time()}]})
    assert error.value.status_code == 401


async def test_deleted_account_rejects_still_valid_jwt():
    class DB:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(deleted_at=time.time()))
    request = Request({"type": "http", "method": "GET", "headers": []})
    with pytest.raises(HTTPException) as error:
        await auth.get_uid(request, {"sub": UID}, DB())
    assert error.value.status_code == 403
