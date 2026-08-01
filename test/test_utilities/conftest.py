"""Shared fixtures for the HTTP-layer tests.

``APIConnector`` and ``OAuth2APIConnector`` issue their requests through
``requests``, so these tests mock at the HTTP layer with the ``requests_mock``
fixture — the outermost boundary we do not own (see
docs/contrib_docs/write_tests.rst).
"""

import pytest

from parsons.utilities.api_connector import APIConnector
from parsons.utilities.oauth_api_connector import OAuth2APIConnector

#: Base uri the connectors under test point at. ``APIConnector`` appends the
#: trailing slash, so ``BASE_URI_NO_SLASH`` is the same uri as a caller might
#: realistically pass it.
BASE_URI = "https://api.example.com/v1/"
BASE_URI_NO_SLASH = "https://api.example.com/v1"

TOKEN_URL = "https://auth.example.com/oauth/token"
CLIENT_ID = "fake-client-id"
CLIENT_SECRET = "fake-client-secret"

DEFAULT_TOKEN_RESPONSE = {"access_token": "fake-token", "token_type": "Bearer"}


@pytest.fixture
def connector():
    """A plain APIConnector pointed at ``BASE_URI``."""
    return APIConnector(BASE_URI)


@pytest.fixture
def oauth_connector(requests_mock):
    """Factory building an OAuth2APIConnector with its token handshake mocked.

    The constructor POSTs to the token endpoint to obtain an access token, so
    that request is registered here. Tests register the data endpoints they
    exercise on the same ``requests_mock`` fixture.

    Keyword arguments are passed through to ``OAuth2APIConnector``; pass
    ``token_response`` to control the payload the token endpoint returns.
    """

    def _build(token_response=None, **kwargs):
        requests_mock.post(TOKEN_URL, json=token_response or DEFAULT_TOKEN_RESPONSE)
        return OAuth2APIConnector(
            BASE_URI,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            token_url=TOKEN_URL,
            auto_refresh_url=None,
            **kwargs,
        )

    return _build
