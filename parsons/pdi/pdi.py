import logging
from datetime import datetime, timezone
from json.decoder import JSONDecodeError

import requests
from dateutil.parser import parse

from parsons import Table
from parsons.pdi.acquisition_types import AcquisitionTypes
from parsons.pdi.activities import Activities
from parsons.pdi.contacts import Contacts
from parsons.pdi.events import Events
from parsons.pdi.flag_ids import FlagIDs
from parsons.pdi.flags import Flags
from parsons.pdi.locations import Locations
from parsons.pdi.questions import Questions
from parsons.pdi.universes import Universes
from parsons.utilities import check_env
from parsons.utilities.api_connector import APIConnector
from parsons.utilities.auth import ExpiringTokenAuth

logger = logging.getLogger(__name__)


class PDI(
    FlagIDs,
    Universes,
    Questions,
    AcquisitionTypes,
    Flags,
    Events,
    Locations,
    Contacts,
    Activities,
):
    def __init__(self, username=None, password=None, api_token=None, qa_url=False):
        """
        Instantiate the PDI class

        Args:
            username: str
                The username for a PDI account. Can be passed as arguement or
                can be set as `PDI_USERNAME` environment variable.
            password: str
                The password for a PDI account. Can be passed as arguement or
                can be set as `PDI_PASSWORD` environment variable.
            api_token: str
                The api_token for a PDI account. Can be passed as arguement or
                can be set as `PDI_API_TOKEN` environment variable.
            qa_url: bool
                Defaults to False. If True, requests will be made to a sandbox
                account. This requires separate qa credentials and api
                token.

        """
        if qa_url:
            self.base_url = "https://apiqa.bluevote.com"
        else:
            self.base_url = "https://api.bluevote.com"

        self.username = check_env.check("PDI_USERNAME", username)
        self.password = check_env.check("PDI_PASSWORD", password)
        self.api_token = check_env.check("PDI_API_TOKEN", api_token)

        # PDI issues a bearer session token from a login endpoint that expires;
        # ExpiringTokenAuth fetches it lazily on the first request and re-fetches
        # before expiry (replacing the hand-rolled token + expiry-check logic).
        self.client = APIConnector(self.base_url, auth=ExpiringTokenAuth(self._fetch_session_token))

        super().__init__()

    def _fetch_session_token(self):
        """Log in and return the session token and its remaining lifetime.

        Returns:
            tuple: ``(token, ttl_seconds)`` for ExpiringTokenAuth.
        """
        login = {
            "Username": self.username,
            "Password": self.password,
            "ApiToken": self.api_token,
        }
        res = requests.post(f"{self.base_url}/sessions", json=login)
        logger.debug(f"{res.status_code} - {res.url}")
        res.raise_for_status()
        data = res.json()
        expiration = parse(data["ExpirationDate"])
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        ttl = (expiration - datetime.now(timezone.utc)).total_seconds()
        return data["AccessToken"], ttl

    def _clean_dict(self, dct):
        if isinstance(dct, list):
            return [self._clean_dict(obj) for obj in dct]

        if isinstance(dct, dict):
            return {k: v for k, v in dct.items() if v is not None}

        return dct

    def _request(self, url, req_type="GET", post_data=None, args=None, limit=None):
        # Based on PDI docs
        # https://api.bluevote.com/docs/index
        LIMIT_MAX = 2000

        if limit and limit <= LIMIT_MAX:
            args = args or {}
            args["limit"] = limit

        args = self._clean_dict(args) if args else args
        post_data = self._clean_dict(post_data) if post_data else post_data
        # The client (via ExpiringTokenAuth) attaches the bearer token and
        # refreshes it as needed; raise_for_status preserves PDI's error handling.
        res = self.client.request(url, req_type, json=post_data, params=args)
        logger.debug(f"{res.url} - {res.status_code}")
        logger.debug(res.request.body)

        res.raise_for_status()

        if not res.text:
            return None

        logger.debug(res.text)

        try:
            res_json = res.json()
        except JSONDecodeError:
            res_json = None

        if "data" not in res_json:
            return res_json

        total_count = res_json.get("totalCount", 0)
        data = res_json["data"]

        # PDI paginates by a 1-indexed "cursor" and reports the total row count.
        # The page size varies in the limit branch, so this count-driven loop
        # is kept rather than a shared paginator.
        if not limit:
            # We don't have a limit, so let's get everything
            # Start a page 2 since we already go page 1
            cursor = 2
            while len(data) < total_count:
                args = args or {}
                args["cursor"] = cursor
                args["limit"] = LIMIT_MAX
                res = self.client.request(url, req_type, json=post_data, params=args)

                data.extend(res.json()["data"])

                cursor += 1

            return Table(data)

        else:
            total_need = min(limit, total_count)

            cursor = 2
            while len(data) < total_need:
                args = args or {}
                args["cursor"] = cursor
                args["limit"] = min(LIMIT_MAX, total_need - len(data))
                res = self.client.request(url, req_type, json=post_data, params=args)

                data.extend(res.json()["data"])

                cursor += 1

            return Table(data)
