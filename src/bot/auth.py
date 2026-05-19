"""Legacy shim - real implementation lives in src.vendors.ticketmaster.auth."""

from __future__ import annotations

from src.vendors.ticketmaster.auth import *  # noqa: F401,F403
from src.vendors.ticketmaster.auth import (  # noqa: F401
    ACCOUNT_URL,
    LOGIN_URL,
    TM_AUTH_COOKIE_NAMES,
    AuthError,
    is_logged_in,
    login,
    wait_for_human_if_captcha,
)
