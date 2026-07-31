import logging
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Literal, overload

import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.auth import AuthBase
from requests.exceptions import HTTPError
from requests.models import PreparedRequest
from simplejson.errors import JSONDecodeError

from parsons import Table
from parsons.utilities.api_exceptions import (
    AuthenticationError,
    ParsonsHTTPError,
    RateLimitError,
)
from parsons.utilities.pagination import PageRequest, Paginator

logger = logging.getLogger(__name__)

_Auth = tuple[str, str] | AuthBase | Callable[[PreparedRequest], PreparedRequest]
_Headers = Mapping[str, str | bytes | None]
_Data = (
    Iterable[bytes]
    | str
    | bytes
    | list[tuple[Any, Any]]
    | tuple[tuple[Any, Any], ...]
    | Mapping[Any, Any]
)
_ParamsMappingKeyType = str | bytes | int | float
_ParamsMappingValueType = str | bytes | int | float | Iterable[str | bytes | int | float] | None
_Params = (
    Mapping[_ParamsMappingKeyType, _ParamsMappingValueType]
    | tuple[_ParamsMappingKeyType, _ParamsMappingValueType]
    | Iterable[tuple[_ParamsMappingKeyType, _ParamsMappingValueType]]
    | str
    | bytes
)

#: The standard timeout for connectors that opt in: 10s to connect, 120s
#: between bytes of the response. The read timeout is between-bytes, not
#: total-duration, so large downloads are unaffected.
DEFAULT_TIMEOUT = (10, 120)

DEFAULT_RETRY_STATUSES = (429, 500, 502, 503, 504)

#: Only methods that are safe to repeat are retried by default. POST is never
#: auto-retried: for many APIs Parsons talks to (donations, signups), a
#: duplicate POST is worse than a failed one.
DEFAULT_RETRY_METHODS = ("GET", "HEAD", "OPTIONS")

# Sentinel distinguishing "not passed" from an explicit timeout=None.
_UNSET = object()

# Module-level alias so tests can substitute a no-op sleep.
_sleep = time.sleep


def default_retry(total: int = 3) -> urllib3.util.Retry:
    """
    Build the standard Parsons retry policy: exponential backoff on
    connection errors and transient status codes (429/5xx), honoring the
    server's Retry-After header on 429s.

    ``raise_on_status=False`` means that when retries are exhausted the final
    response is returned as usual and ``validate_response()`` raises the same
    ``HTTPError`` it always has — enabling retries changes no error handling.

    Args:
        total: int
            Maximum number of retries. Defaults to 3.

    Returns:
        urllib3.util.Retry
    """
    return urllib3.util.Retry(
        total=total,
        backoff_factor=1,
        status_forcelist=DEFAULT_RETRY_STATUSES,
        allowed_methods=DEFAULT_RETRY_METHODS,
        respect_retry_after_header=True,
        raise_on_status=False,
    )


class APIConnector:
    """
    Low level class for API requests that other connectors can utilize.

    It is understood that there are many standards for REST APIs and
    it will be difficult to create a universal connector.
    The goal of this class is create series of utilities that can be
    mixed and matched to, hopefully, meet the needs of the specific API.

    """

    def __init__(
        self,
        uri: str,
        headers: _Headers | None = None,
        auth: _Auth | None = None,
        pagination_key: str | None = None,
        data_key: str | None = None,
        *,
        timeout: int | float | tuple | None = None,
        retries: int | urllib3.util.Retry | None = None,
        rate_limit_interval: int | float = 0.0,
        session: requests.Session | None = None,
    ) -> None:
        """
        Initialize the APIConnector.

        Args:
            uri:
                The base uri for the api.
                Must include a trailing ``/``.
                E.g. ``http://myapi.com/v1/``.
            headers: The request headers
            auth: The request authorization parameters
            pagination_key:
                The name of the key in the response json
                where the pagination url is located.
                Required for pagination.
            data_key:
                The name of the key in the response json
                where the data is contained.
                Required if the data is nested in the response json.
            timeout:
                Seconds before a request times out, either a single number or a
                ``(connect, read)`` tuple (see ``DEFAULT_TIMEOUT``). Defaults to
                ``None`` (no timeout) for backwards compatibility.
            retries:
                Retry transient failures automatically. Pass an int for the
                standard policy (see :func:`default_retry`) with that many
                retries, or a ``urllib3.util.Retry`` for full control. Defaults
                to ``None`` (no retries).
            rate_limit_interval:
                Minimum seconds between requests, for APIs with strict rate
                limits. Defaults to ``0`` (no throttling).
            session:
                A session for all requests to be made through. Defaults to a new
                ``requests.Session``; ``OAuth2APIConnector`` passes its OAuth2
                session here.

        """
        # Add a trailing slash if its missing
        if not uri.endswith("/"):
            uri = uri + "/"

        self.uri = uri
        self.headers = headers
        self.auth = auth
        self.pagination_key = pagination_key
        self.data_key = data_key
        self.timeout = timeout
        self.rate_limit_interval = rate_limit_interval
        self._last_request_at = None
        self.session = session if session is not None else requests.Session()

        if retries is not None:
            retry = retries if isinstance(retries, urllib3.util.Retry) else default_retry(retries)
            # Configure the session's existing adapters rather than mounting
            # fresh ones, so a caller-injected session keeps any custom
            # adapters (TLS, tuned pools, proxies) it had at http(s)://.
            for adapter in set(self.session.adapters.values()):
                if isinstance(adapter, HTTPAdapter):
                    adapter.max_retries = retry

    def request(
        self,
        url: str,
        req_type: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        *,
        json: Any | None = None,
        data: _Data | None = None,
        params: _Params | None = None,
        headers: _Headers | None = None,
        timeout: Any = _UNSET,
        raise_on_error: bool = True,
        **kwargs,
    ) -> requests.Response:
        """
        Base request using requests libary.

        Args:
            url:
                The url request string.
                If ``url`` is a relative URL,
                it will be joined with the ``uri`` of the ``APIConnector`.
                If ``url`` is an absolute URL,
                it will be used as is.
            req_type: The request type.
            json:
                The payload of the request object.
                By using json, it will automatically serialize the dictionary.
            data:
                The payload of the request object.
                Use instead of json in some instances.
            params:
                The parameters to append to the url.
                E.g. ``http://myapi.com/things?id=1``
            headers:
                Headers for this request only, merged over the connector's headers.
            timeout:
                Timeout for this request only, overriding the connector's timeout.
            raise_on_error:
                If the request yields an error status code (anything above 400),
                raise an error. In most cases, this should be ``True``,
                however in some cases, if you are looping through data,
                you might want to ignore individual failures.
            `**kwargs`:
                Additional keyword arguments to pass to
                :meth:`requests.Session.request` (e.g. ``files=``, ``stream=``).

        """
        full_url = urllib.parse.urljoin(self.uri, url)

        merged_headers = self.headers
        if headers:
            merged_headers = {**(self.headers or {}), **headers}

        self._throttle()

        resp = self.session.request(
            req_type,
            full_url,
            headers=merged_headers,
            auth=self.auth,
            json=json,
            data=data,
            params=params,
            timeout=self.timeout if timeout is _UNSET else timeout,
            **kwargs,
        )

        if raise_on_error:
            self.validate_response(resp)

        return resp

    def get(self, url: str, *, params: _Params | None = None, **kwargs) -> requests.Response:
        """
        Make a GET request and return the validated response.

        Raises a :class:`~parsons.utilities.api_exceptions.ParsonsHTTPError`
        subclass on an error status code. Call ``.json()`` on the returned
        response for the parsed body.

        Args:
            url: A relative or absolute url for the api request.
            params: The request parameters.
            `**kwargs`: Additional arguments passed through to :meth:`request`.

        Returns:
            requests.Response

        """
        return self.request(url, "GET", params=params, **kwargs)

    def post(
        self,
        url: str,
        *,
        params: _Params | None = None,
        data: _Data | None = None,
        json: Any | None = None,
        **kwargs,
    ) -> requests.Response:
        """Make a POST request and return the validated response (see :meth:`get`)."""
        return self.request(url, "POST", params=params, data=data, json=json, **kwargs)

    def put(
        self,
        url: str,
        *,
        params: _Params | None = None,
        data: _Data | None = None,
        json: Any | None = None,
        **kwargs,
    ) -> requests.Response:
        """Make a PUT request and return the validated response (see :meth:`get`)."""
        return self.request(url, "PUT", params=params, data=data, json=json, **kwargs)

    def patch(
        self,
        url: str,
        *,
        params: _Params | None = None,
        data: _Data | None = None,
        json: Any | None = None,
        **kwargs,
    ) -> requests.Response:
        """Make a PATCH request and return the validated response (see :meth:`get`)."""
        return self.request(url, "PATCH", params=params, data=data, json=json, **kwargs)

    def delete(
        self,
        url: str,
        *,
        params: _Params | None = None,
        data: _Data | None = None,
        json: Any | None = None,
        **kwargs,
    ) -> requests.Response:
        """Make a DELETE request and return the validated response (see :meth:`get`)."""
        return self.request(url, "DELETE", params=params, data=data, json=json, **kwargs)

    def paginate(
        self,
        url: str,
        paginator: Paginator,
        *,
        params: _Params | None = None,
        max_pages: int | None = None,
        **kwargs,
    ) -> Iterator[requests.Response]:
        """
        Make a GET request and follow the pagination strategy until the last
        page, yielding each validated response.

        See :mod:`parsons.utilities.pagination` for the available strategies.

        .. code-block:: python

            connector = APIConnector("https://api.example.com/v1/")
            paginator = LinkHeaderPaginator()
            members = []
            for response in connector.paginate("members", paginator):
                members.extend(response.json()["members"])

        Args:
            url: A relative or absolute url for the first page's request.
            paginator: The pagination strategy, e.g. ``LinkHeaderPaginator()``.
            params: The request parameters for the first page.
            max_pages:
                If provided, stop after this many pages as a safety valve
                against endless pagination.
            `**kwargs`: Additional arguments passed through to :meth:`request`.

        Returns:
            Iterator of requests.Response, one per page.

        """
        page = PageRequest(url, params)
        pages_fetched = 0
        while page is not None:
            if max_pages is not None and pages_fetched >= max_pages:
                logger.warning("Stopping pagination after max_pages=%s pages.", max_pages)
                return
            response = self.get(page.url, params=page.params, **kwargs)
            yield response
            pages_fetched += 1
            page = paginator.next_page(response, page)

    @overload
    def get_request(
        self,
        url: ...,
        *,
        params: ... = ...,
        return_format: Literal["json"] = "json",
        raise_on_error: ... = ...,
        **kwargs,
    ) -> dict[str, Any]: ...

    @overload
    def get_request(
        self,
        url: ...,
        *,
        params: ... = ...,
        return_format: Literal["content"],
        raise_on_error: ... = ...,
        **kwargs,
    ) -> bytes: ...

    def get_request(
        self,
        url: str,
        *,
        params: _Params | None = None,
        return_format: Literal["json", "content"] = "json",
        raise_on_error: bool = True,
        **kwargs,
    ) -> dict | bytes:
        """
        Make a GET request.

        Args:
            url: A complete and valid url for the api request.
            params: The request parameters.
            raise_on_error:
                If the request yields an error status code (anything above 400),
                raise an error. In most cases, this should be ``True``,
                however in some cases, if you are looping through data,
                you might want to ignore individual failures.
            `**kwargs`:
                Additional keyword arguments to pass to :meth:`request`.

        Returns:
            The :meth:`requests.Response.json` from the response if `return_format` is ``json``,
            or :attr:`requests.Response.content` from the response if `return_format` is ``content``.

        Raises:
            RuntimeError: If return_format is not ``json`` or ``content``.

        """
        r = self.request(url, "GET", params=params, raise_on_error=raise_on_error, **kwargs)

        if return_format == "json":
            return r.json()

        if return_format == "content":
            return r.content

        raise RuntimeError(f"{return_format} is not a valid format, change to json or content")

    def post_request(
        self,
        url: str,
        *,
        params: _Params | None = None,
        data: _Data | None = None,
        json: Any | None = None,
        success_codes: list[int] | None = None,
        raise_on_error: bool = True,
        **kwargs,
    ) -> dict[str, Any] | int | None:
        """
        Make a POST request.

        Args:
            url: A complete and valid url for the api request
            params: The request parameters
            data: A data object to post
            json: A JSON object to post
            success_codes:
                The expected success code to be returned.
                If not provided, accepts 200, 201, 202, and 204.
            raise_on_error:
                If the request yields an error status code (anything above 400),
                raise an error.
            `**kwargs`:
                Additional keyword arguments to pass to :meth:`request`.

        Returns:
            If successful, json data from :meth:`requests.Response.json`
            or :attr:`requests.Response.status_code` as available.
            ``None`` if the request fails and `raise_on_error` is ``False``.

        """
        r = self.request(
            url,
            "POST",
            params=params,
            data=data,
            json=json,
            raise_on_error=raise_on_error,
            **kwargs,
        )

        if success_codes is None:
            success_codes = [200, 201, 202, 204]

        if r.status_code in success_codes:
            if self.json_check(r):
                return r.json()

            return r.status_code

    def delete_request(
        self,
        url: str,
        *,
        params: _Params | None = None,
        success_codes: list[int] | None = None,
        raise_on_error: bool = True,
        **kwargs,
    ) -> dict[str, Any] | int | None:
        """
        Make a DELETE request.

        Args:
            url: A complete and valid url for the api request
            params: The request parameters
            success_codes:
                The expected success codes to be returned.
                If not provided, accepts 200, 201, 202, 204.
            raise_on_error:
                If the request yields an error status code (anything above 400),
                raise an error.
            `**kwargs`:
                Additional keyword arguments to pass to :meth:`request`.

        Returns:
            If successful, json data from :meth:`requests.Response.json`
            or :attr:`requests.Response.status_code` as available.
            ``None`` if the request fails and `raise_on_error` is ``False``.

        """
        r = self.request(url, "DELETE", params=params, raise_on_error=raise_on_error, **kwargs)

        if success_codes is None:
            success_codes = [200, 201, 202, 204]

        if r.status_code in success_codes:
            if self.json_check(r):
                return r.json()

            return r.status_code

    def put_request(
        self,
        url: str,
        *,
        data: _Data | None = None,
        json: Any | None = None,
        params: _Params | None = None,
        success_codes: list[int] | None = None,
        raise_on_error: bool = True,
        **kwargs,
    ) -> dict[str, Any] | int | None:
        """
        Make a PUT request.

        Args:
            url: A complete and valid url for the api request
            data: A data object to post
            json: A JSON object to post
            params: The request parameters
            success_codes:
                The expected success codes to be returned.
                If not provided, accepts 200, 201, 202, 204.
            raise_on_error:
                If the request yields an error status code (anything above 400),
                raise an error.
            `**kwargs`:
                Additional keyword arguments to pass to :meth:`request`.

        Returns:
            If successful, json data from :meth:`requests.Response.json`
            or :attr:`requests.Response.status_code` as available.
            ``None`` if the request fails and `raise_on_error` is ``False``.

        """
        r = self.request(
            url, "PUT", params=params, data=data, json=json, raise_on_error=raise_on_error, **kwargs
        )

        if success_codes is None:
            success_codes = [200, 201, 202, 204]

        if r.status_code in success_codes:
            if self.json_check(r):
                return r.json()

            return r.status_code

    def patch_request(
        self,
        url: str,
        *,
        params: _Params | None = None,
        data: _Data | None = None,
        json: Any | None = None,
        success_codes: list[int] | None = None,
        raise_on_error: bool = True,
        **kwargs,
    ) -> dict[str, Any] | int | None:
        """
        Make a PATCH request.

        Args:
            url: A complete and valid url for the api request
            params: The request parameters
            data: A data object to post
            json: A JSON object to post
            success_codes:
                The expected success codes to be returned.
                If not provided, accepts 200, 201, 202, and 204.
            raise_on_error:
                If the request yields an error status code (anything above 400),
                raise an error.
            `**kwargs`:
                Additional keyword arguments to pass to :meth:`request`.

        Returns:
            If successful, json data from :meth:`requests.Response.json`
            or :attr:`requests.Response.status_code` as available.
            ``None`` if the request fails and `raise_on_error` is ``False``.

        """
        r = self.request(
            url,
            "PATCH",
            params=params,
            data=data,
            json=json,
            raise_on_error=raise_on_error,
            **kwargs,
        )

        if success_codes is None:
            success_codes = [200, 201, 202, 204]

        if r.status_code in success_codes:
            if self.json_check(r):
                return r.json()

            return r.status_code

    def validate_response(self, resp: requests.Response) -> None:
        """
        Validate that the response is not an error code.

        If it is, then raise an error and display the error message. The error
        is a subclass of ``requests.exceptions.HTTPError`` (see
        :mod:`parsons.utilities.api_exceptions`) with the response attached as
        ``.response``; a 429 raises ``RateLimitError`` and a 401 raises
        ``AuthenticationError``.

        """
        try:
            resp.raise_for_status()

        except HTTPError as e:
            message = f"Code: {resp.status_code}; URL: {resp.url}"

            if resp.reason:
                message = f"{message}; Reason: {resp.reason}"

            elif resp.text:
                message = f"{message}; Text: {resp.text}"

            # Some errors return JSONs with useful info about the error.
            if self.json_check(resp):
                message = f"{message}; JSON: {resp.json()}"

            if resp.status_code == 429:
                error_class = RateLimitError
            elif resp.status_code == 401:
                error_class = AuthenticationError
            else:
                error_class = ParsonsHTTPError

            raise error_class(message, response=resp) from e

    @overload
    def data_parse(self, resp: dict[str, Any]) -> dict[str, Any]: ...

    @overload
    def data_parse(self, resp: list) -> list: ...

    def data_parse(self, resp: dict[str, Any] | list) -> dict[str, Any] | list:
        """
        Determines if the response json has nested data.

        If it is nested, it just returns the data.
        This is useful in dealing with requests that might return multiple records,
        while others might return only a single record.

        """
        # TODO(jburchard): Some response jsons are enclosed in a list.
        # Need to deal with unpacking and/or not assuming that it is going to be a dict.

        # In some instances responses are just lists.
        if isinstance(resp, list):
            return resp

        if self.data_key and self.data_key in resp:
            return resp[self.data_key]

        return resp

    # There are many different ways in which APIs indicate whether there is a next page
    # of data following the initial request. The goal is build out a series of utilities
    # that mean most of the most common use cases.

    def next_page_check_url(self, resp: dict[str, Any]) -> bool:
        """
        Check to determine if there is a next page.

        This requires that the response json contains a pagination key
        that is empty if there is not a next page.

        """
        if self.pagination_key and self.pagination_key in resp:
            return bool(resp[self.pagination_key])

        return False

    def json_check(self, resp: requests.Response) -> bool:
        """Check to see if a response has a json included in it."""
        try:
            resp.json()
            return True

        except JSONDecodeError:
            return False

    def convert_to_table(self, data: list | Any) -> Table:
        """Internal method to create a Parsons table from a data element."""
        return Table(data) if isinstance(data, list) else Table([data])

    def _throttle(self) -> None:
        """Sleep as needed to keep ``rate_limit_interval`` seconds between requests."""
        if self.rate_limit_interval <= 0:
            return
        if self._last_request_at is not None:
            wait = self._last_request_at + self.rate_limit_interval - time.monotonic()
            if wait > 0:
                _sleep(wait)
        self._last_request_at = time.monotonic()
