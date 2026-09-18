"""Security middleware for the local admin UI.

Admin UI plan, Phase 11 (§14): even though the UI binds to localhost by
default, browser-facing security still matters. This module provides:

* **Host validation** (§14.3) -- reject requests whose ``Host`` header is
  not one of the allowed hosts, which defends the local admin service
  against DNS-rebinding attacks (a malicious page resolving its own
  domain to 127.0.0.1 and POSTing to the admin API).
* **CSRF protection** (§14.2) -- a double-submit token required on every
  state-changing (unsafe) request, so a cross-origin page cannot forge an
  authenticated POST/DELETE.

Authentication is deliberately *not* included: the first release is
localhost-only and single-user (§14.6). Remote exposure must introduce
its own auth model before it is allowed.
"""

from __future__ import annotations

import hmac
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
CSRF_COOKIE = "ragmonk_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"


def _allowed_hosts(host: str, port: int) -> frozenset[str]:
    """Host header values accepted for a given bind host/port.

    Loopback names and the bound address, with and without the port.
    A non-loopback bind (a future ``--host 0.0.0.0`` deployment) still
    validates against its own configured host, but such a deployment is
    expected to add its own real security model first (§14.5).
    """
    names = {"localhost", "127.0.0.1", "[::1]", "::1", host}
    values: set[str] = set()
    for name in names:
        if not name:
            continue
        values.add(name)
        values.add(f"{name}:{port}")
    return frozenset(values)


class HostValidationMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Host header is not in the allowed set."""

    def __init__(self, app: ASGIApp, *, host: str, port: int) -> None:
        super().__init__(app)
        self._allowed = _allowed_hosts(host, port)

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        host_header = request.headers.get("host", "")
        if host_header and host_header not in self._allowed:
            return PlainTextResponse(
                "Host not allowed (DNS-rebinding protection).", status_code=421
            )
        return await call_next(request)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit-cookie CSRF protection for unsafe methods.

    A random token is set as a cookie on the first response and must be
    echoed back on every unsafe request via either the ``X-CSRF-Token``
    header or a ``csrf_token`` query parameter. The token is validated
    against the cookie value (double-submit): a cross-origin page cannot
    read the cookie, so it cannot forge a matching token.

    The token is read from the header/query, never the request body --
    reading ``request.form()`` inside ``BaseHTTPMiddleware`` would consume
    the body stream and break the downstream route's own form parsing. The
    UI's mutating actions are issued by HTMX with a global ``hx-headers``
    that attaches the ``X-CSRF-Token`` header, so every unsafe request
    carries the token without touching the body.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        cookie_token = request.cookies.get(CSRF_COOKIE)

        if request.method not in SAFE_METHODS:
            submitted = request.headers.get(CSRF_HEADER) or request.query_params.get(
                CSRF_FORM_FIELD
            )
            if (
                cookie_token is None
                or submitted is None
                or not hmac.compare_digest(cookie_token, submitted)
            ):
                return PlainTextResponse("CSRF validation failed.", status_code=403)

        response: Response = await call_next(request)
        if cookie_token is None:
            new_token = secrets.token_urlsafe(32)
            response.set_cookie(
                CSRF_COOKIE,
                new_token,
                httponly=False,  # the template reads it to echo it back
                samesite="strict",
                secure=False,  # localhost is plain HTTP
                path="/",
            )
            # Make the freshly minted token available to the template on
            # this same response via request state.
            request.state.csrf_token = new_token
        return response


def current_csrf_token(request: Request) -> str:
    """The CSRF token to embed in templates for this request.

    Prefers a token minted on this request (first visit) over the cookie.
    """
    token = getattr(request.state, "csrf_token", None)
    if token:
        return str(token)
    return request.cookies.get(CSRF_COOKIE, "")
