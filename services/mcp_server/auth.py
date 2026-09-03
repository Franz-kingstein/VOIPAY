import sys
import os
from typing import Dict, Any
from fastapi import HTTPException, Header, status

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from shared.auth.jwt_utils import decode_access_token

def verify_token_from_header(authorization: str = Header(...)) -> Dict[str, Any]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header must start with Bearer",
        )
    token = authorization.split(" ")[1]
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication token",
        )
    return payload
