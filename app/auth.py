from pathlib import Path

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from firebase_admin import auth, credentials, initialize_app, get_app
from app.config import get_settings

settings = get_settings()

bearer_scheme = HTTPBearer()


def init_firebase():
    try:
        get_app()
    except ValueError:
        cred = credentials.Certificate(Path(__file__).parent.parent / settings.firebase_credentials_path)
        initialize_app(cred, {"projectId": settings.firebase_project_id})


init_firebase()


async def get_uid(
    token: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    try:
        decoded = auth.verify_id_token(token.credentials)
        return decoded["uid"]
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )