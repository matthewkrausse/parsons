"""Typed exceptions raised by the Parsons HTTP layer.

All exceptions subclass ``requests.exceptions.HTTPError`` with the same
message format previously raised by ``APIConnector.validate_response``, so
existing ``except HTTPError`` handlers keep working. The offending
``requests.Response`` is attached as ``.response``.
"""

import datetime
import math
from email.utils import parsedate_to_datetime

from requests.exceptions import HTTPError


class ParsonsHTTPError(HTTPError):
    """An HTTP response with an error status code (>= 400)."""


class AuthenticationError(ParsonsHTTPError):
    """The server rejected the request's credentials (HTTP 401)."""


class RateLimitError(ParsonsHTTPError):
    """The server throttled the request (HTTP 429)."""

    @property
    def retry_after(self) -> float | None:
        """
        Seconds the server asked us to wait before retrying, if it sent a
        Retry-After header. Handles both the delta-seconds and HTTP-date
        forms of the header.

        Returns:
            float or None
        """
        if self.response is None:
            return None

        value = self.response.headers.get("Retry-After")
        if value is None:
            return None

        try:
            seconds = float(value)
            return max(0.0, seconds) if math.isfinite(seconds) else None
        except ValueError:
            pass

        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None

        # A date without a zone (e.g. a bare time or a "-0000" offset) parses
        # to a naive datetime; treat it as UTC so the subtraction below works.
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)

        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (retry_at - now).total_seconds())
