"""Two independent gates: network origin, then a signed session cookie.

The network gate is the important one. Even with auth disabled, the app only
answers requests whose source address falls inside app.allowed_networks.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
import secrets
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

log = logging.getLogger(__name__)

COOKIE = "plexcleaner_session"
CSRF_COOKIE = "plexcleaner_csrf"
PUBLIC_PATHS = {"/login", "/healthz", "/static"}


class NetworkGuard(BaseHTTPMiddleware):
    """Reject anything originating outside the configured internal networks."""

    def __init__(self, app, allowed_networks: list, trust_proxy_header: bool = False):
        super().__init__(app)
        self.allowed = allowed_networks
        self.trust_proxy_header = trust_proxy_header

    def client_ip(self, request: Request) -> str:
        if self.trust_proxy_header:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else ""

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        raw = self.client_ip(request)
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            log.warning("rejecting request with unparseable source address %r", raw)
            return PlainTextResponse("Forbidden", status_code=403)
        if not any(ip in net for net in self.allowed):
            log.warning("rejecting request from %s (outside allowed networks)", ip)
            return PlainTextResponse("Forbidden: this service is internal only.", status_code=403)
        return await call_next(request)


class SessionAuth(BaseHTTPMiddleware):
    """Signed-cookie session with a shared password, plus CSRF on writes."""

    def __init__(self, app, secret_key: str, password: str, max_age_seconds: int):
        super().__init__(app)
        self.enabled = bool(password)
        self.password = password
        self.max_age = max_age_seconds
        self.serializer = URLSafeTimedSerializer(secret_key or secrets.token_hex(32), salt="plexcleaner")

    def make_token(self) -> str:
        return self.serializer.dumps({"ok": True})

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            self.serializer.loads(token, max_age=self.max_age)
            return True
        except (BadSignature, SignatureExpired):
            return False

    def check_password(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate or "", self.password)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/healthz" or any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)

        if self.enabled and not self.valid(request.cookies.get(COOKIE)):
            if path.startswith("/api/"):
                return JSONResponse({"error": "authentication required"}, status_code=401)
            return RedirectResponse("/login", status_code=302)

        # CSRF: state-changing requests must echo the double-submit cookie.
        # Enforced even with no password set — otherwise any page a browser on
        # the LAN loads could POST a deletion here on the user's behalf.
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            cookie_token = request.cookies.get(CSRF_COOKIE, "")
            header_token = request.headers.get("x-csrf-token", "")
            if not cookie_token or not hmac.compare_digest(cookie_token, header_token):
                return JSONResponse({"error": "CSRF token missing or invalid"}, status_code=403)

        return await call_next(request)


def issue_csrf(response: Any) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(CSRF_COOKIE, token, httponly=False, samesite="strict", path="/")
    return token
