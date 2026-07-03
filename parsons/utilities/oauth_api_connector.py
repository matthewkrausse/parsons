from typing import Literal

from oauthlib.oauth2 import BackendApplicationClient, TokenExpiredError
from requests_oauthlib import OAuth2Session

from parsons.utilities.api_connector import APIConnector


class OAuth2APIConnector(APIConnector):
    """
    The OAuth2API Connector is a low level class for authenticated API requests using OAuth2.
    It extends APIConnector by running all requests through a server-side OAuth2 session
    and otherwise provides the same interface as APIConnector.

    Args:
        uri: str
            The base uri for the api. Must include a trailing '/' (e.g. ``http://myapi.com/v1/``)
        client_id: str
            The client id for acquiring and exchanging tokens from the OAuth2 application
        client_secret: str
            The client secret for acquiring and exchanging tokens  from the OAuth2 application
        token_url: str
            The URL for acquiring new tokens from the OAuth2 Application
        auto_refresh_url: str
            If provided, the URL for refreshing tokens from the OAuth2 Application
        headers: dict
            The request headers
        pagination_key: str
            The name of the key in the response json where the pagination url is
            located. Required for pagination.
        data_key: str
            The name of the key in the response json where the data is contained. Required
            if the data is nested in the response json
        timeout: int or float or tuple
            Seconds before a request times out. See ``APIConnector``.
        retries: int or urllib3.util.Retry
            Retry transient failures automatically. See ``APIConnector``.
        rate_limit_interval: int or float
            Minimum seconds between requests. See ``APIConnector``.

    Returns:
        OAuthAPIConnector class

    """

    def __init__(
        self,
        uri: str,
        client_id: str,
        client_secret: str,
        token_url: str,
        auto_refresh_url: str | None,
        headers: dict[str, str] | None = None,
        pagination_key: str | None = None,
        data_key: str | None = None,
        grant_type: str = "client_credentials",
        authorization_kwargs: dict[str, str] | None = None,
        *,
        timeout=None,
        retries=None,
        rate_limit_interval=0.0,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.grant_type = grant_type
        self.authorization_kwargs = authorization_kwargs or {}
        self._fetch_timeout = timeout

        self.token = self._fetch_token()
        self.client = OAuth2Session(
            client_id,
            token=self.token,
            auto_refresh_url=auto_refresh_url,
            token_updater=self.token_saver,
            auto_refresh_kwargs=self.authorization_kwargs,
        )

        super().__init__(
            uri,
            headers=headers,
            pagination_key=pagination_key,
            data_key=data_key,
            timeout=timeout,
            retries=retries,
            rate_limit_interval=rate_limit_interval,
            session=self.client,
        )

    def request(
        self,
        url,
        req_type: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        json=None,
        data=None,
        params=None,
        **kwargs,
    ):
        """
        Base request; see ``APIConnector.request``. If the OAuth2 token has
        expired, a fresh one is fetched and the request is retried. The
        expiry is detected client-side before anything is sent, so the retry
        is safe for all request types.
        """
        try:
            return super().request(url, req_type, json=json, data=data, params=params, **kwargs)
        except TokenExpiredError:
            self.token = self._fetch_token()
            self.client.token = self.token
            return super().request(url, req_type, json=json, data=data, params=params, **kwargs)

    def _fetch_token(self) -> dict:
        """Fetch a fresh token from the OAuth2 application."""
        client = BackendApplicationClient(client_id=self.client_id)
        client.grant_type = self.grant_type
        oauth = OAuth2Session(client=client)
        # authorization_kwargs takes precedence: historically a token-fetch
        # timeout could only be set by passing authorization_kwargs={"timeout": ...},
        # so setdefault avoids a duplicate-keyword TypeError for that pattern.
        fetch_kwargs = dict(self.authorization_kwargs)
        fetch_kwargs.setdefault("timeout", self._fetch_timeout)
        return oauth.fetch_token(
            token_url=self.token_url,
            client_id=self.client_id,
            client_secret=self.client_secret,
            **fetch_kwargs,
        )

    def token_saver(self, token):
        self.token = token
