import os
import json
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
        print(f"Get App Succeeded")
    except ValueError:
        firebase_credentials_json = os.environ.get('FIREBASE_CREDENTIALS_JSON')
        print(f"CREDENTIALS LENGTH: {len(firebase_credentials_json) if firebase_credentials_json else 'NONE'}")
        print(f"FIRST 50 CHARS: {firebase_credentials_json[:50] if firebase_credentials_json else 'NONE'}")

        if firebase_credentials_json:
            cred = credentials.Certificate(json.loads(firebase_credentials_json))
        else:
            cred = credentials.Certificate(Path(__file__).parent.parent / settings.firebase_credentials_path)

        initialize_app(cred, {"projectId": settings.firebase_project_id})


init_firebase()

async def get_uid(
        token: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> str:
    try:
        decoded = auth.verify_id_token(token.credentials)
        return decoded["uid"]
    except Exception as e:
        print(f"Token verification failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )