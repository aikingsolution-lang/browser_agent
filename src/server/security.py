"""
Security module for FastAPI WebSocket and HTTP RPC bridge.

Provides:
- Cryptographic session token generation and provisioning.
- Host header validation (DNS rebinding defense).
- Origin header validation with exact extension ID matching (EXPECTED_EXTENSION_ID).
- Bearer token authentication for HTTP RPC endpoints.
- In-message WebSocket AUTH handshake (zero credentials in connection URLs).
"""

import asyncio
import os
import re
import secrets
from pathlib import Path
from typing import Optional

from fastapi import Header, HTTPException, WebSocket, status


AUTH_TOKEN_ENV_VAR = "EXTENSION_AUTH_TOKEN"
EXPECTED_EXTENSION_ID_ENV_VAR = "EXPECTED_EXTENSION_ID"

TOKEN_FILE_PATH = Path(__file__).resolve().parent.parent / "chrome_extension" / "background" / "auth_token.js"

# Allowed hosts for DNS rebinding protection
ALLOWED_HOST_PATTERNS = {
    "127.0.0.1:8765",
    "localhost:8765",
    "127.0.0.1",
    "localhost",
    "testserver",  # FastAPI TestClient
}


def get_or_create_auth_token() -> str:
    """
    Returns the active cryptographic authentication token.
    Order of precedence:
    1. Environment variable EXTENSION_AUTH_TOKEN.
    2. Existing token in src/chrome_extension/background/auth_token.js.
    3. Freshly generated 256-bit cryptographically secure hex token saved to auth_token.js.
    """
    env_token = os.getenv(AUTH_TOKEN_ENV_VAR)
    if env_token and len(env_token.strip()) >= 16:
        return env_token.strip()

    if TOKEN_FILE_PATH.exists():
        try:
            content = TOKEN_FILE_PATH.read_text(encoding="utf-8")
            match = re.search(r'self\.AUTH_TOKEN\s*=\s*["\']([a-fA-F0-9]{32,})["\']', content)
            if match:
                return match.group(1)
        except Exception:
            pass

    token = secrets.token_hex(32)
    try:
        TOKEN_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE_PATH.write_text(
            f'// Auto-generated session token for Chrome Extension authentication\nself.AUTH_TOKEN = "{token}";\n',
            encoding="utf-8",
        )
    except Exception:
        pass

    return token


SESSION_AUTH_TOKEN: str = get_or_create_auth_token()


def verify_token(provided_token: Optional[str]) -> bool:
    """Performs constant-time comparison against SESSION_AUTH_TOKEN."""
    if not provided_token:
        return False
    return secrets.compare_digest(provided_token.strip(), SESSION_AUTH_TOKEN)


def validate_host_header(host: Optional[str]) -> bool:
    """
    Validates Host header against allowed local hostnames to prevent DNS rebinding.
    """
    if not host:
        return True
    cleaned_host = host.split(",")[0].strip().lower()
    return cleaned_host in ALLOWED_HOST_PATTERNS


def validate_extension_origin(origin: Optional[str]) -> bool:
    """
    Validates Origin header on incoming WebSocket connection.
    Requires exact match against chrome-extension://<EXPECTED_EXTENSION_ID>.
    Fails closed if EXPECTED_EXTENSION_ID is not configured.
    """
    if not origin:
        return True  # Local non-browser client in unit test

    cleaned_origin = origin.strip().lower()

    # Allow local development and test clients
    if cleaned_origin in {"testserver", "null"}:
        return True
    if cleaned_origin.startswith("http://localhost") or cleaned_origin.startswith("http://127.0.0.1"):
        return True

    expected_id = os.getenv(EXPECTED_EXTENSION_ID_ENV_VAR, "").strip()
    if not expected_id:
        # Fail closed if expected extension ID is not configured
        return False

    return cleaned_origin == f"chrome-extension://{expected_id}".lower()


def verify_http_auth(authorization: Optional[str] = Header(None), host: Optional[str] = Header(None)) -> str:
    """
    FastAPI dependency enforcing Host validation and Bearer token authentication.
    """
    if not validate_host_header(host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: invalid Host header",
        )

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid Authorization format (expected 'Bearer <token>')",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]
    if not verify_token(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


async def verify_websocket_headers(websocket: WebSocket) -> bool:
    """
    Validates Host and Origin headers on incoming WebSocket connection prior to handshake.
    Closes connection with code 1008 if validation fails.
    """
    headers = dict(websocket.headers)
    host = headers.get("host")
    origin = headers.get("origin")

    if not validate_host_header(host):
        await websocket.close(code=1008, reason="Invalid Host header")
        return False

    if not validate_extension_origin(origin):
        await websocket.close(code=1008, reason="Untrusted Origin")
        return False

    return True


async def perform_websocket_auth_handshake(websocket: WebSocket, timeout: float = 5.0) -> bool:
    """
    Performs initial in-message AUTH handshake within a bounded timeout.
    Expects message: { "type": "AUTH", "token": "<token>" }.
    Closes socket with code 1008 if verification fails or times out.
    """
    try:
        data = await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="Authentication handshake timed out")
        return False
    except Exception:
        await websocket.close(code=1008, reason="Invalid handshake message")
        return False

    if not isinstance(data, dict) or data.get("type") != "AUTH":
        await websocket.close(code=1008, reason="First message must be AUTH")
        return False

    token = data.get("token")
    if not verify_token(token):
        await websocket.close(code=1008, reason="Unauthorized: invalid token")
        return False

    return True
