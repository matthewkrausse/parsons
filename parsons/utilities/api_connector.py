import logging
import time
import urllib.parse
from collections.abc import Iterator
from typing import Literal

import requests
import urllib3
from requests.adapters import HTTPAdapter
from simplejson.errors import JSONDecodeError

from parsons import Table
from parsons.utilities.api_exceptions import (
    AuthenticationError,
    ParsonsHTTPError,
    RateLimitError,
)
from parsons.utilities.pagination import PageRequest, Paginator

logger = logging.getLogger(__name__)

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
    The API Connector is a low level class for API requests that other connectors
    can utilize. It is understood that there are many standards for REST APIs and it will be
    difficult to create a universal connector. The goal of this class is create series
    of utilities that can be mixed and matched to, hopefully, meet the needs of the specific
    API.

    Args:
        uri: str
            The base uri for the api. Must include a trailing '/' (e.g. ``http://myapi.com/v1/``)
        headers: dict
            The request headers
        auth: dict
            The request authorization parameters
        pagination_key: str
            The name of the key in the response json where the pagination url is
            located. Required for pagination.
        data_key: str
            The name of the key in the response json where the data is contained. Required
            if the data is nested in the response json
        timeout: int or float or tuple
            Seconds before a request times out, either a single number or a
            ``(connect, read)`` tuple (see ``DEFAULT_TIMEOUT``). Defaults to ``None``
            (no timeout) for backwards compatibility.
        retries: int or urllib3.util.Retry
            Retry transient failures automatically. Pass an int for the standard
            policy (see :func:`default_retry`) with that many retries, or a
            ``urllib3.util.Retry`` for full control. Defaults to ``None`` (no retries).
        rate_limit_interval: int or float
            Minimum seconds between requests, for APIs with strict rate limits.
            Defaults to ``0`` (no throttling).
        session: requests.Session
            A session for all requests to be made through. Defaults to a new
            ``requests.Session``; ``OAuth2APIConnector`` passes its OAuth2 session here.

    Returns:
        APIConnector class

    """

    def __init__(
        self,
        uri,
        headers=None,
        auth=None,
        pagination_key=None,
        data_key=None,
        *,
        timeout=None,
        retries=None,
        rate_limit_interval=0.0,
        session=None,
    ):
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
        url,
        req_type: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        json=None,
        data=None,
        params=None,
        headers=None,
        timeout=_UNSET,
        **kwargs,
    ):
        """
        Base request using requests libary.

        Args:
            url: str
                The url request string; if ``url`` is a relative URL, it will be joined with
                the ``uri`` of the ``APIConnector`; if ``url`` is an absolute URL, it will
                be used as is.
            req_type: str
                The request type. One of GET, POST, PUT, PATCH, DELETE, OPTIONS
            json: dict
                The payload of the request object. By using json, it will automatically
                serialize the dictionary
            data: str or byte or dict
                The payload of the request object. Use instead of json in some instances.
            params: dict
                The parameters to append to the url (e.g. http://myapi.com/things?id=1)
            headers: dict
                Headers for this request only, merged over the connector's headers.
            timeout: int or float or tuple
                Timeout for this request only, overriding the connector's timeout.
            **kwargs:
                Additional arguments passed through to ``requests`` (e.g. ``files=``,
                ``stream=``).

        Returns:
            requests response

        """
        full_url = urllib.parse.urljoin(self.uri, url)

        merged_headers = self.headers
        if headers:
            merged_headers = {**(self.headers or {}), **headers}

        self._throttle()

        return self.session.request(
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

    def get(self, url, *, params=None, **kwargs) -> requests.Response:
        """
        Make a GET request and return the validated response.

        Raises a :class:`~parsons.utilities.api_exceptions.ParsonsHTTPError`
        subclass on an error status code. Call ``.json()`` on the returned
        response for the parsed body.

        Args:
            url: str
                A relative or absolute url for the api request
            params: dict
                The request parameters
            **kwargs:
                Additional arguments passed through to :meth:`request`
        Returns:
            requests.Response

        """
        resp = self.request(url, "GET", params=params, **kwargs)
        self.validate_response(resp)
        return resp

    def post(self, url, *, params=None, data=None, json=None, **kwargs) -> requests.Response:
        """
        Make a POST request and return the validated response.

        Args:
            url: str
                A relative or absolute url for the api request
            params: dict
                The request parameters
            data: str or file
                A data object to post
            json: dict
                A JSON object to post
            **kwargs:
                Additional arguments passed through to :meth:`request`
        Returns:
            requests.Response

        """
        resp = self.request(url, "POST", params=params, data=data, json=json, **kwargs)
        self.validate_response(resp)
        return resp

    def put(self, url, *, params=None, data=None, json=None, **kwargs) -> requests.Response:
        """
        Make a PUT request and return the validated response.

        Args:
            url: str
                A relative or absolute url for the api request
            params: dict
                The request parameters
            data: str or file
                A data object to put
            json: dict
                A JSON object to put
            **kwargs:
                Additional arguments passed through to :meth:`request`
        Returns:
            requests.Response

        """
        resp = self.request(url, "PUT", params=params, data=data, json=json, **kwargs)
        self.validate_response(resp)
        return resp

    def patch(self, url, *, params=None, data=None, json=None, **kwargs) -> requests.Response:
        """
        Make a PATCH request and return the validated response.

        Args:
            url: str
                A relative or absolute url for the api request
            params: dict
                The request parameters
            data: str or file
                A data object to patch
            json: dict
                A JSON object to patch
            **kwargs:
                Additional arguments passed through to :meth:`request`
        Returns:
            requests.Response

        """
        resp = self.request(url, "PATCH", params=params, data=data, json=json, **kwargs)
        self.validate_response(resp)
        return resp

    def delete(self, url, *, params=None, data=None, json=None, **kwargs) -> requests.Response:
        """
        Make a DELETE request and return the validated response.

        Args:
            url: str
                A relative or absolute url for the api request
            params: dict
                The request parameters
            data: str or file
                A data object to send
            json: dict
                A JSON object to send
            **kwargs:
                Additional arguments passed through to :meth:`request`
        Returns:
            requests.Response

        """
        resp = self.request(url, "DELETE", params=params, data=data, json=json, **kwargs)
        self.validate_response(resp)
        return resp

    def paginate(
        self, url, paginator: Paginator, *, params=None, max_pages=None, **kwargs
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
            url: str
                A relative or absolute url for the first page's request
            paginator: Paginator
                The pagination strategy, e.g. ``LinkHeaderPaginator()``
            params: dict
                The request parameters for the first page
            max_pages: int
                If provided, stop after this many pages as a safety valve
                against endless pagination.
            **kwargs:
                Additional arguments passed through to :meth:`request`
        Returns:
            Iterator of requests.Response, one per page

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

    def get_request(self, url, params=None, return_format="json"):
        """
        Make a GET request.

        Args:
            url: str
                A complete and valid url for the api request
            params: dict
                The request parameters
        Returns:
                A requests response object

        """

        r = self.request(url, "GET", params=params)
        self.validate_response(r)

        if return_format == "json":
            logger.debug(r.json())
            return r.json()
        elif return_format == "content":
            return r.content
        else:
            raise RuntimeError(f"{return_format} is not a valid format, change to json or content")

    def post_request(self, url, params=None, data=None, json=None, success_codes=None):
        """
        Make a POST request.

        Args:
            url: str
                A complete and valid url for the api request
            params: dict
                The request parameters
            data: str or file
                A data object to post
            json: dict
                A JSON object to post
            success_codes: int
                The expected success code to be returned. If not provided, accepts 200, 201, 202, and 204.

        Returns:
            A requests response object

        """

        if success_codes is None:
            success_codes = [200, 201, 202, 204]
        r = self.request(url, "POST", params=params, data=data, json=json)

        # Validate the response and lift up an errors.
        self.validate_response(r)

        # Check for a valid success code for the POST. Some APIs return messages with the
        # success code and some do not. Be able to account for both of these types.
        if r.status_code in success_codes:
            if self.json_check(r):
                return r.json()
            else:
                return r.status_code

    def delete_request(self, url, params=None, success_codes=None):
        """
        Make a DELETE request.

        Args:
            url: str
                A complete and valid url for the api request
            params: dict
                The request parameters
            success_codes: int
                The expected success codes to be returned. If not provided, accepts 200, 201, 204.

        Returns:
                A requests response object or status code

        """

        if success_codes is None:
            success_codes = [200, 201, 204]
        r = self.request(url, "DELETE", params=params)

        self.validate_response(r)

        # Check for a valid success code for the POST. Some APIs return messages with the
        # success code and some do not. Be able to account for both of these types.
        if r.status_code in success_codes:
            if self.json_check(r):
                return r.json()
            else:
                return r.status_code

    def put_request(self, url, data=None, json=None, params=None, success_codes=None):
        """
        Make a PUT request.

        Args:
            url: str
                A complete and valid url for the api request
            data: str or file
                A data object to post
            json: dict
                A JSON object to post
            params: dict
                The request parameters
            success_codes: int
                The expected success codes to be returned. If not provided, accepts 200, 201, 204.

        Returns:
                A requests response object

        """

        if success_codes is None:
            success_codes = [200, 201, 204]
        r = self.request(url, "PUT", params=params, data=data, json=json)

        self.validate_response(r)

        if r.status_code in success_codes:
            if self.json_check(r):
                return r.json()
            else:
                return r.status_code

    def patch_request(self, url, params=None, data=None, json=None, success_codes=None):
        """
        Make a PATCH request.

        Args:
            url: str
                A complete and valid url for the api request
            params: dict
                The request parameters
            data: str or file
                A data object to post
            json: dict
                A JSON object to post
            success_codes: int
                The expected success codes to be returned. If not provided, accepts 200, 201, and 204.

        Returns:
            A requests response object

        """

        if success_codes is None:
            success_codes = [200, 201, 204]
        r = self.request(url, "PATCH", params=params, data=data, json=json)

        self.validate_response(r)

        # Check for a valid success code for the POST. Some APIs return messages with the
        # success code and some do not. Be able to account for both of these types.
        if r.status_code in success_codes:
            if self.json_check(r):
                return r.json()
            else:
                return r.status_code

    def validate_response(self, resp):
        """
        Validate that the response is not an error code. If it is, then raise an error
        and display the error message.

        The error raised is a subclass of ``requests.exceptions.HTTPError``
        (see :mod:`parsons.utilities.api_exceptions`) with the response
        attached as ``.response``.

        Args:
            resp: object
                A response object

        """

        if resp.status_code >= 400:
            if resp.reason:
                message = f"HTTP error occurred ({resp.status_code}): {resp.reason}"
            elif resp.text:
                message = f"HTTP error occurred ({resp.status_code}): {resp.text}"
            else:
                message = f"HTTP error occurred ({resp.status_code})"

            if resp.status_code == 429:
                error_class = RateLimitError
            elif resp.status_code == 401:
                error_class = AuthenticationError
            else:
                error_class = ParsonsHTTPError

            # Some errors return JSONs with useful info about the error. Return it if exists.
            if self.json_check(resp):
                raise error_class(f"{message}, json: {resp.json()}", response=resp)
            else:
                raise error_class(message, response=resp)

    def data_parse(self, resp):
        """
        Determines if the response json has nested data. If it is nested, it just returns the
        data. This is useful in dealing with requests that might return multiple records, while
        others might return only a single record.

        Args:
            resp:
                A response dictionary
        Returns:
            dict
                A dictionary of data.

        """

        # TODO: Some response jsons are enclosed in a list. Need to deal with unpacking and/or
        # not assuming that it is going to be a dict.

        # In some instances responses are just lists.
        if isinstance(resp, list):
            return resp

        if self.data_key and self.data_key in resp:
            return resp[self.data_key]
        else:
            return resp

    # There are many different ways in which APIs indicate whether there is a next page
    # of data following the initial request. The goal is build out a series of utilities
    # that mean most of the most common use cases.

    def next_page_check_url(self, resp):
        """
        Check to determine if there is a next page. This requires that the response json
        contains a pagination key that is empty if there is not a next page.

        Args:
            resp:
                A response dictionary
        `Returns:
            boolean

        """

        if self.pagination_key and self.pagination_key in resp:
            if resp[self.pagination_key]:
                return True
        else:
            return False

    def json_check(self, resp):
        """Check to see if a response has a json included in it."""

        try:
            resp.json()
            return True
        except JSONDecodeError:
            return False

    def convert_to_table(self, data):
        """Internal method to create a Parsons table from a data element."""
        table = None
        table = Table(data) if type(data) is list else Table([data])

        return table

    def _throttle(self):
        """Sleep as needed to keep ``rate_limit_interval`` seconds between requests."""
        if self.rate_limit_interval <= 0:
            return
        if self._last_request_at is not None:
            wait = self._last_request_at + self.rate_limit_interval - time.monotonic()
            if wait > 0:
                _sleep(wait)
        self._last_request_at = time.monotonic()
