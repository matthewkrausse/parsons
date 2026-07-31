"""Pagination strategies for ``APIConnector.paginate()``.

Each paginator implements a single method, ``next_page(response, request)``,
which derives the next page's request from the previous response and the
request that produced it — or returns ``None`` when there are no more pages.
Paginators hold no per-iteration state, so one instance can be shared across
concurrent pagination loops.

The four strategies cover the patterns used by Parsons connectors:

- :class:`LinkHeaderPaginator` — next URL in the RFC 5988 ``Link`` response
  header (Freshdesk, Shopify).
- :class:`NextUrlPaginator` — next URL in the response body, addressed by a
  dotted key path (Capitol Canary ``pagination.next_url``, Action Network
  ``_links.next.href``, Mobilize America ``next``).
- :class:`CursorPaginator` — opaque cursor in the response body that is sent
  back as a query parameter (Airmeet, Hustle, Zoom).
- :class:`PageNumberPaginator` — incrementing page-number query parameter,
  stopping on an empty/short page or on a boolean "more pages" flag in the
  body (Action Builder, Action Network, QuickBooks Time).
"""

import urllib.parse
from dataclasses import dataclass
from typing import Any, Protocol

import requests


@dataclass
class PageRequest:
    """The URL and query parameters for one page request.

    ``params`` of ``None`` means the URL already carries everything it needs
    (e.g. a next-page URL returned by the server).
    """

    url: str
    params: dict | None = None


class Paginator(Protocol):
    """Protocol implemented by all pagination strategies."""

    def next_page(
        self, response: requests.Response, request: PageRequest
    ) -> PageRequest | None: ...


def _dig(data: Any, dotted_key: str) -> Any:
    """Extract a value from nested dicts via a dotted key path ('_links.next.href')."""
    current = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _is_truthy(value: Any) -> bool:
    """Interpret a "there are more pages" flag.

    Handles APIs that return the flag as a real boolean as well as those that
    return a stringy boolean; ``False``, ``None``, ``0``, ``""``, and the
    strings ``"false"``/``"0"``/``"no"`` (any case) all mean "no more pages".
    """
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no")
    return bool(value)


class LinkHeaderPaginator:
    """Follow the next-page URL in the RFC 5988 ``Link`` response header.

    Args:
        rel: str
            The link relation to follow. Defaults to ``"next"``.
    """

    def __init__(self, rel: str = "next"):
        self.rel = rel

    def next_page(self, response: requests.Response, request: PageRequest) -> PageRequest | None:
        url = response.links.get(self.rel, {}).get("url")
        if not url:
            return None
        # A relative next URL resolves against the page that returned it
        # (RFC 3986/5988), not the connector base. The next URL carries its
        # own query string, so the original params are not re-sent on top.
        return PageRequest(urllib.parse.urljoin(response.url, url))


class NextUrlPaginator:
    """Follow a next-page URL found in the response body.

    Args:
        next_key: str
            Dotted key path to the next-page URL within the response JSON,
            e.g. ``"next"``, ``"pagination.next_url"``, or
            ``"_links.next.href"``. Pagination stops when the key is missing
            or its value is empty/null.
    """

    def __init__(self, next_key: str = "next"):
        self.next_key = next_key

    def next_page(self, response: requests.Response, request: PageRequest) -> PageRequest | None:
        next_url = _dig(response.json(), self.next_key)
        if not next_url:
            return None
        # A relative next URL resolves against the page that returned it
        # (RFC 3986), not the connector base.
        return PageRequest(urllib.parse.urljoin(response.url, next_url))


class CursorPaginator:
    """Send back an opaque cursor from the response body as a query parameter.

    Args:
        cursor_key: str
            Dotted key path to the cursor within the response JSON, e.g.
            ``"cursors.after"``.
        cursor_param: str
            The query parameter name the cursor is sent back in.
        more_key: str
            Optional dotted key path to a "there are more pages" flag. When
            set, it is the stop condition (evaluated truthy-aware, so a stringy
            ``"false"`` stops); use it for APIs that keep returning a cursor
            even on the last page. When omitted, pagination stops as soon as
            the cursor is missing or empty.
    """

    def __init__(self, cursor_key: str, cursor_param: str, more_key: str | None = None):
        self.cursor_key = cursor_key
        self.cursor_param = cursor_param
        self.more_key = more_key

    def next_page(self, response: requests.Response, request: PageRequest) -> PageRequest | None:
        body = response.json()
        if self.more_key is not None and not _is_truthy(_dig(body, self.more_key)):
            return None
        cursor = _dig(body, self.cursor_key)
        if not cursor:
            return None
        return PageRequest(request.url, {**(request.params or {}), self.cursor_param: cursor})


class PageNumberPaginator:
    """Increment a page-number query parameter until the pages run out.

    Two ways to detect the last page, in order of preference:

    - ``more_key`` — a dotted path to a boolean "there are more pages" flag in
      the response body (e.g. QuickBooks Time ``"more"``, or a ``"has_more"``
      style flag). When the flag is falsy, pagination stops. This is exact and
      needs no request beyond the last page.
    - otherwise, the page's item list is inspected: pagination stops on an
      empty page, or (when ``page_size`` is set) on a page shorter than
      ``page_size``.

    Args:
        page_param: str
            The query parameter carrying the page number. Defaults to
            ``"page"``.
        start_page: int
            The page number the first request is assumed to have used when
            ``page_param`` is absent from its params. Defaults to ``1``.
        data_key: str
            Dotted key path to the list of items within the response JSON.
            Omit if the response body is itself the list. Ignored when
            ``more_key`` is set.
        page_size: int
            If provided, pagination also stops when a page holds fewer than
            this many items, saving a final empty-page request. Ignored when
            ``more_key`` is set.
        more_key: str
            Dotted key path to a boolean "more pages" flag; when set, it is the
            stop condition and ``data_key``/``page_size`` are not consulted.
    """

    def __init__(
        self,
        page_param: str = "page",
        start_page: int = 1,
        data_key: str | None = None,
        page_size: int | None = None,
        more_key: str | None = None,
    ):
        self.page_param = page_param
        self.start_page = start_page
        self.data_key = data_key
        self.page_size = page_size
        self.more_key = more_key

    def next_page(self, response: requests.Response, request: PageRequest) -> PageRequest | None:
        body = response.json()
        if self.more_key is not None:
            if not _is_truthy(_dig(body, self.more_key)):
                return None
        else:
            items = _dig(body, self.data_key) if self.data_key else body
            if items is None:
                return None
            if not isinstance(items, list):
                raise TypeError(
                    f"PageNumberPaginator expected a list at data_key={self.data_key!r} but got "
                    f"{type(items).__name__}; set data_key to the key holding the page's items."
                )
            if not items:
                return None
            if self.page_size is not None and len(items) < self.page_size:
                return None

        params = dict(request.params or {})
        current_page = int(params.get(self.page_param, self.start_page))
        params[self.page_param] = current_page + 1
        return PageRequest(request.url, params)
