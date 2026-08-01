"""Pluggable authentication helpers for the Parsons HTTP layer.

These are standard ``requests.auth.AuthBase`` implementations, so they work
with ``APIConnector(auth=...)``, ``OAuth2APIConnector``, or any plain
``requests`` call.
"""

import math
import time
from collections.abc import Callable

from requests.auth import AuthBase


class HeaderTokenAuth(AuthBase):
    """Attach a static token to a request header.

    Args:
        token: str
            The token value.
        header: str
            The header to set. Defaults to ``"Authorization"``.
        template: str
            Format string for the header value, with the token interpolated
            as ``{token}``. Defaults to ``"Bearer {token}"``; use
            ``"{token}"`` for APIs that expect the bare token.
    """

    def __init__(self, token: str, header: str = "Authorization", template: str = "Bearer {token}"):
        self.token = token
        self.header = header
        self.template = template

    def __call__(self, r):
        r.headers[self.header] = self.template.format(token=self.token)
        return r


class ExpiringTokenAuth(AuthBase):
    """Token auth with lazy fetch and expiry-margin refresh.

    The token is fetched on the first request and re-fetched
    ``refresh_margin`` seconds *before* it expires, so a request is never
    sent with a token that is about to lapse (and no failed request needs to
    be replayed). This replaces hand-rolled fetch-token-then-track-expiry
    code in individual connectors.

    Either pass a ``fetch_token`` callable or subclass and override
    :meth:`fetch_token`. It must return a ``(token, ttl_seconds)`` tuple;
    use a ``ttl_seconds`` of ``None`` for tokens that never expire.

    Args:
        fetch_token: Callable
            Zero-argument callable returning ``(token, ttl_seconds)``.
        refresh_margin: float
            Refresh this many seconds before expiry. Defaults to ``60``.
        header: str
            The header to set. Defaults to ``"Authorization"``.
        template: str
            Format string for the header value. Defaults to
            ``"Bearer {token}"``.
    """

    def __init__(
        self,
        fetch_token: Callable[[], tuple[str, float | None]] | None = None,
        *,
        refresh_margin: float = 60.0,
        header: str = "Authorization",
        template: str = "Bearer {token}",
    ):
        if fetch_token is not None:
            self.fetch_token = fetch_token
        self.refresh_margin = refresh_margin
        self.header = header
        self.template = template
        self._token: str | None = None
        self._expires_at = 0.0

    def fetch_token(self) -> tuple[str, float | None]:
        """Fetch a fresh token. Override in a subclass or pass ``fetch_token=``."""
        raise NotImplementedError(
            "Pass fetch_token= to ExpiringTokenAuth or subclass it and override fetch_token()."
        )

    def __call__(self, r):
        expired = math.isfinite(self._expires_at) and (
            time.monotonic() >= self._expires_at - self.refresh_margin
        )
        if self._token is None or expired:
            token, ttl = self.fetch_token()
            self._token = token
            self._expires_at = math.inf if ttl is None else time.monotonic() + ttl
        r.headers[self.header] = self.template.format(token=self._token)
        return r
