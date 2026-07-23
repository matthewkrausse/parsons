# Migrating connectors onto the standard `APIConnector`

This is a working guide for moving Parsons' REST connectors onto the shared
HTTP layer ([`APIConnector`](../parsons/utilities/README.md)) so they all make
consistent calls — with pooled connections, timeouts, retries, `Retry-After`
handling, and shared pagination — instead of each hand-rolling those concerns.

It doubles as the **status tracker** for the effort. Once every connector has
migrated, this file can be removed.

> New to the HTTP layer itself? Read the [layer reference](../parsons/utilities/README.md)
> first; this guide assumes it.

## Why

The shared client used to be a thin wrapper over `requests.request()`: no
`Session`, no timeout, no retries, and inconsistent return types. Connectors
compensated by hand-rolling pagination (four different ways), token refresh,
and rate-limit `sleep()`s. The refactor hardens the client and gives connectors
one way to do each of these; migrating a connector means **deleting** its
bespoke plumbing in favor of the shared primitives.

## Status tracker

Legend: ✅ merged · 🔵 POC (this batch) · ⬜ not started · ⛔ out of scope (vendor SDK / non-HTTP).

| Connector | Auth family | Pagination | Status | PR |
|---|---|---|---|---|
| freshdesk | basic-auth | link-header | ✅ | #3 |
| quickbooks | header-token | page-number | ✅ | #3 |
| capitol_canary | basic-auth | next-url-in-body | 🔵 | — |
| hustle | oauth2 | cursor | 🔵 | — |
| pdi | expiring-token | page-number | 🔵 | — |
| crowdtangle | api-key-param | next-url-in-body | 🔵 | — |
| actblue | basic-auth | polling | 🔵 | — |
| targetsmart | header-token | none | 🔵 | — |
| action_builder | header-token | page-number | ⬜ | — |
| action_network | header-token | page-number | ⬜ | — |
| action_kit | basic-auth | next-url-in-body | ⬜ | — |
| airmeet | expiring-token | cursor | ⬜ | — |
| auth0 | oauth2 | polling | ⬜ | — |
| bill_com | expiring-token | page-number | ⬜ | — |
| bloomerang | header-token / oauth2 | page-number | ⬜ | — |
| catalist | oauth2 | polling | ⬜ | — |
| census | api-key-param | none | ⬜ | — |
| community | header-token | none | ⬜ | — |
| controlshift | oauth2 | page-number | ⬜ | — |
| copper | header-token | page-number | ⬜ | — |
| donorbox | basic-auth | page-number | ⬜ | — |
| empower | header-token | none | ⬜ | — |
| formstack | header-token | page-number | ⬜ | — |
| mailchimp | basic-auth | page-number | ⬜ | — |
| mobilecommons | header-token | page-number (XML) | ⬜ | — |
| mobilize_america | header-token (optional) | next-url-in-body | ⬜ | — |
| nation_builder | header-token | next-url-in-body | ⬜ | — |
| newmode | oauth2 | next-url-in-body | ⬜ | — |
| ngpvan | basic-auth | next-url-in-body | ⬜ | — |
| phone2action | basic-auth | next-url-in-body | ⬜ | — |
| quickbase | header-token | none | ⬜ | — |
| redash | header-token (`Key`) | polling | ⬜ | — |
| rockthevote | api-key-param | polling | ⬜ | — |
| scytl | none | none | ⬜ | — |
| shopify | header-token | link-header | ⬜ | — |
| sisense | header-token | none | ⬜ | — |
| turbovote | expiring-token | none | ⬜ | — |
| zoom | oauth2 | cursor | ⬜ | — |
| airtable, alchemer, aws, azure, box, braintree, civis, facebook_ads, geocode, github, google, notifications, salesforce, twilio | — | — | ⛔ | vendor SDK |
| databases, etl, sftp | — | — | ⛔ | non-HTTP |

## The classification

Every HTTP connector falls into one **auth family** and one **pagination
family**. Migrating is mostly a matter of looking up the row and applying the
mapped helper + paginator.

**Auth family → what to use**

| Auth family | Meaning | Use |
|---|---|---|
| basic-auth | user/password or key-as-user tuple | keep `auth=(...)` / `HTTPBasicAuth` |
| header-token | static token in a header | `HeaderTokenAuth(token, header=..., template=...)` |
| expiring-token | token fetched from a login endpoint, expires | `ExpiringTokenAuth(fetch_token)` |
| oauth2 | client-credentials grant | `OAuth2APIConnector` |
| api-key-param | key in the query string | leave it a query param (no auth object) |
| none | no auth (public files) | nothing |

**Pagination family → what to use**

| Pagination family | Meaning | Use |
|---|---|---|
| link-header | next URL in the `Link:` header | `LinkHeaderPaginator(rel="next")` |
| next-url-in-body | next URL in the JSON body | `NextUrlPaginator("dotted.path")` |
| cursor | opaque cursor echoed as a query param | `CursorPaginator(cursor_key, cursor_param)` |
| page-number | incrementing page number | `PageNumberPaginator(page_param, ...)` |
| polling | submit a job, poll until ready | keep the loop connector-side (no paginator) |
| none | single request | nothing |

See the [layer reference](../parsons/utilities/README.md) for the exact
paginator signatures.

## The migration recipe

1. **Look up the connector** in the status tracker (auth family, pagination
   family).
2. **Set auth** on the `APIConnector`: keep the `auth=` tuple for basic-auth;
   use `HeaderTokenAuth` / `ExpiringTokenAuth` for token headers; switch to
   `OAuth2APIConnector` for client-credentials; leave API keys as query params.
   Delete any hand-rolled token-fetch/refresh code the helper now covers.
3. **Replace the pagination loop** with `paginate()`:
   ```python
   rows = []
   for response in self.client.paginate(endpoint, <Paginator>, params=params):
       rows.extend(response.json()[<data key>])
   ```
4. **Swap request calls** to the verb methods where returning a `Response` is
   cleaner (`.json()`), or leave existing `*_request` calls as-is (they still
   work).
5. **Add reliability config** where the connector hand-rolled it: map a manual
   `time.sleep(n)` between calls to `rate_limit_interval=n`; pass `timeout=`
   / `retries=` if the connector wants them ahead of the fleet-wide default flip.
6. **Regression gate:** the connector's **existing** tests must pass
   *unmodified*. If a migration forces you to change an assertion in an existing
   test, that is evidence of an unintended behavior change — stop and
   reconcile.
7. **Add one pagination test**: multiple pages concatenate, and the page /
   cursor / URL advances correctly.
8. **One connector per PR.** Declare it breaking or non-breaking (it should be
   non-breaking).

### Worked examples

Two connectors are already migrated on branch `api-connector-refactor` (PR #3)
— read their diffs as templates:

- **freshdesk** (`git show 0fc5faa8d9`) — basic-auth tuple × Link header. A
  regex `while "link" in headers` loop becomes
  `paginate(endpoint, LinkHeaderPaginator())`.
- **quickbooks** (`git show fdd5cd1e40`) — Bearer header × page number. A manual
  `page += 1 while response["more"]` loop becomes
  `paginate(end_point, PageNumberPaginator(more_key="more"))`.

## POC batch

Before the broad waves, this batch migrates one connector from each auth ×
pagination combination, so every shared primitive is proven end-to-end on a
real connector:

| POC | Proves (auth) | Proves (pagination) | Also exercises |
|---|---|---|---|
| freshdesk ✅ | keep basic-auth tuple | `LinkHeaderPaginator` | — |
| quickbooks ✅ | `HeaderTokenAuth` (Bearer) | `PageNumberPaginator(more_key)` | body "more" flag |
| capitol_canary | keep `HTTPBasicAuth` | `NextUrlPaginator` | cleaner stop condition |
| hustle | `OAuth2APIConnector` | `CursorPaginator` | deletes hand-rolled token+refresh |
| pdi | `ExpiringTokenAuth` | `PageNumberPaginator` | login-body token, expiry refresh |
| crowdtangle | API key stays a query param | `NextUrlPaginator` (nested) | `rate_limit_interval` |
| actblue | keep basic-auth tuple | none — polling loop | verb methods around an async job |
| targetsmart | `HeaderTokenAuth` (custom header) | none | the simplest possible migration |

### Per-connector notes (POC batch)

- **capitol_canary** — keep `HTTPBasicAuth`; replace `_paginate_request` with
  `NextUrlPaginator("pagination.next_url")`. Confirm `next_url` is null on the
  last page (this replaces the fragile legacy `count == per_page` stop).
- **hustle** — replace `_get_auth_token` / `_refresh_token` with
  `OAuth2APIConnector` (client-credentials); replace the loop with
  `CursorPaginator("pagination.cursor", "cursor")`, `data_key="items"`. Note:
  hustle signals the end via `pagination.hasNextPage == "true"` (a **string**)
  while the cursor may stay populated, so this migration adds a truthy-aware
  stop-flag option to `CursorPaginator` (mirroring `PageNumberPaginator.more_key`).
- **pdi** — wrap the `POST /sessions` login (Username/Password/ApiToken →
  AccessToken + ExpirationDate) in `ExpiringTokenAuth.fetch_token`, parsing
  ExpirationDate for the refresh margin;
  `PageNumberPaginator(page_param="cursor", start_page=1, page_size=LIMIT_MAX, data_key="data")`
  (the short-final-page heuristic covers the `totalCount` stop).
- **crowdtangle** — the key stays a query param (no auth helper);
  `NextUrlPaginator("result.pagination.nextPage")`; set
  `rate_limit_interval=REQUEST_SLEEP` to replace the manual 10s sleep. Data
  lives under `result.<dynamic-first-key>`.
- **actblue** — swap `post_request` / `get_request` for `post` / `get`
  returning a `Response`; **keep** `poll_for_download_url` (no paginator
  applies); the final CSV is still fetched via `Table.from_csv(download_url)`.
- **targetsmart** — `HeaderTokenAuth(api_key, header="x-api-key", template="{token}")`;
  no pagination. The sibling modules `targetsmart_smartmatch` (S3 upload) and
  `targetsmart_automation` (SFTP) are non-HTTP and out of scope.

## After the POC: the waves

Once the POC batch has merged and the paginator/auth patterns are proven, the
rest migrate in difficulty order, **one connector per PR**:

- **Wave A — existing `APIConnector` users** (easy): they already compose the
  client; the PR just deletes hand-rolled pagination/token/throttle code in
  favor of the shared primitives.
- **Wave B — raw-`requests` connectors** (medium): move onto `APIConnector`;
  some need `**kwargs` passthrough (`files=`, `stream=`).
- **Wave C — hard cases** (minimal ambition): large surfaces (`action_kit`),
  polling models (`redash`), or non-JSON (`scytl`, `mobilecommons` XML) adopt
  the shared transport (timeouts/retries) first, without a full rewrite.

## Reviewer rules

- A migration PR that **forces an assertion change in an existing test** is
  evidence of an unintended behavior change. Investigate before merging.
- **One connector per PR** — never batch.
- Any change to a **shared primitive** (a new paginator option, etc.) is
  additive, lands with its own unit tests in
  `test/test_utilities/test_api_connector_features.py`, and is its own PR ahead
  of the connector that needs it.
- Every PR declares **breaking / non-breaking** (migrations should be
  non-breaking).

## See also

- [`parsons/utilities/README.md`](../parsons/utilities/README.md) — the HTTP layer reference.
- The overall refactor plan (sessions/timeouts/retries, the default-on flips,
  and the full wave sequencing) lives with the effort's design notes.
