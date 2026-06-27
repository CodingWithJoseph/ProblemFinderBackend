import os
import json
import logging
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import auth, credentials, initialize_app, get_app, firestore

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)
bearer_scheme = HTTPBearer(auto_error=False)


def init_firebase():
    try:
        get_app()
        return
    except ValueError:
        pass

    firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')

    if firebase_credentials_json:
        cred = credentials.Certificate(json.loads(firebase_credentials_json))
    else:
        cred = credentials.Certificate(
            Path(__file__).parent.parent / settings.firebase_credentials_path
        )

    # verify_id_token validates the token's `aud`/`iss` against the app's
    # project_id, so it MUST equal the project that issued the token. Tokens are
    # issued by the project the service account belongs to, so the credential's
    # own project_id is authoritative. Prefer it over FIREBASE_PROJECT_ID, which
    # is a hand-set env var that can silently drift — a mismatch makes
    # verify_id_token reject otherwise-valid tokens with an "invalid audience"
    # error that surfaces to clients as "Invalid or expired token".
    cred_project_id = getattr(cred, "project_id", None)
    project_id = cred_project_id or settings.firebase_project_id

    if (
        cred_project_id
        and settings.firebase_project_id
        and cred_project_id != settings.firebase_project_id
    ):
        logger.warning(
            "FIREBASE_PROJECT_ID (%s) does not match the service-account project "
            "(%s); using the credential's project for token verification.",
            settings.firebase_project_id,
            cred_project_id,
        )

    initialize_app(cred, {"projectId": project_id})


init_firebase()


def get_firestore():
    return firestore.client()


async def get_uid(
    request: Request,
    token: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    if not token:
        # Distinct from a verification failure: no bearer token was sent at all
        # (e.g. a curl call missing the `Authorization: Bearer <token>` header).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication credentials",
        )
    try:
        # clock_skew_seconds tolerates small clock differences between this
        # server and Google's token-issuing servers, which would otherwise
        # reject freshly-minted tokens as "used too early" / prematurely expired.
        decoded = auth.verify_id_token(token.credentials, clock_skew_seconds=10)
        uid = decoded["uid"]
        request.state.uid = uid
        return uid
    except Exception as e:
        logger.info("token verification failed: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
