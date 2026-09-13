"""
Single-user access control for the dashboard.

This page can close positions, so it is not open by default. A token is read
from ``WEB_UI_TOKEN``; if that is unset one is generated once and written to
``data/web_token.txt`` with owner-only permissions, then printed to the log so
it can be copied. Comparison is constant time.

``WEB_UI_AUTH=off`` disables the check entirely, which is only reasonable on a
loopback bind reached over something like Tailscale.
"""

import logging
import os
import secrets

from config.settings import WEB_UI_AUTH, WEB_UI_TOKEN

logger = logging.getLogger(__name__)

TOKEN_FILE = "data/web_token.txt"
COOKIE_NAME = "theta_desk"
_token = None


def auth_required() -> bool:
    return WEB_UI_AUTH != "off"


def get_token() -> str:
    """The access token, generated and saved once if it was never configured."""
    global _token
    if _token:
        return _token
    if WEB_UI_TOKEN:
        _token = WEB_UI_TOKEN
        return _token

    if os.path.exists(TOKEN_FILE):
        try:
            saved = open(TOKEN_FILE).read().strip()
            if saved:
                _token = saved
                return _token
        except OSError as e:
            logger.warning(f"⚠️ Could not read {TOKEN_FILE}: {e}")

    _token = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(TOKEN_FILE) or ".", exist_ok=True)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(_token)
        logger.warning(f"🔑 Dashboard access token generated → {TOKEN_FILE}")
    except OSError as e:
        logger.error(f"❌ Could not save the dashboard token: {e}")
    return _token


def token_ok(candidate: str) -> bool:
    if not candidate:
        return False
    return secrets.compare_digest(str(candidate), get_token())


def request_ok(request) -> bool:
    """True when this request carries a valid cookie or bearer token."""
    if not auth_required():
        return True
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        if token_ok(header[7:].strip()):
            return True
    return token_ok(request.cookies.get(COOKIE_NAME, ""))
