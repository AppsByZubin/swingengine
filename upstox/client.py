"""Minimal Upstox HTTP client for token requests and verification."""

from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import quote

import requests

from upstox.config import UpstoxSettings


class UpstoxAPIError(RuntimeError):
    """Raised for a sanitized Upstox API failure."""


@dataclass(frozen=True)
class TokenRequest:
    authorization_expiry: int


class UpstoxAuthClient:
    """Call only the endpoints required by the token-rotation workflow."""

    def __init__(
        self,
        settings: UpstoxSettings,
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self._session = session or requests.Session()
        self._request_lock = Lock()

    def request_access_token(self) -> TokenRequest:
        client_id = quote(self.settings.api_key, safe="")
        url = (
            f"{self.settings.api_base_url}/v3/login/auth/token/request/"
            f"{client_id}"
        )
        try:
            with self._request_lock:
                response = self._session.post(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json={"client_secret": self.settings.api_secret},
                    timeout=self.settings.request_timeout_seconds,
                )
        except requests.RequestException as error:
            raise UpstoxAPIError("Upstox token request could not be reached") from error

        if response.status_code != 200:
            raise UpstoxAPIError(
                f"Upstox token request returned HTTP {response.status_code}"
            )

        payload = self._json_object(response, "token request")
        if payload.get("status") != "success" or not isinstance(
            payload.get("data"), dict
        ):
            raise UpstoxAPIError("Upstox token request returned an invalid response")
        try:
            authorization_expiry = int(payload["data"]["authorization_expiry"])
        except (KeyError, TypeError, ValueError) as error:
            raise UpstoxAPIError(
                "Upstox token request omitted authorization_expiry"
            ) from error
        return TokenRequest(authorization_expiry=authorization_expiry)

    def verify_access_token(self, access_token: str) -> None:
        url = f"{self.settings.api_base_url}/v2/user/profile"
        try:
            with self._request_lock:
                response = self._session.get(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Api-Version": "2.0",
                        "Authorization": f"Bearer {access_token}",
                    },
                    timeout=self.settings.request_timeout_seconds,
                )
        except requests.RequestException as error:
            raise UpstoxAPIError(
                "Upstox token verification could not be reached"
            ) from error

        if response.status_code != 200:
            raise UpstoxAPIError(
                f"Upstox token verification returned HTTP {response.status_code}"
            )
        payload = self._json_object(response, "token verification")
        data: Any = payload.get("data")
        if payload.get("status") != "success" or not isinstance(data, dict):
            raise UpstoxAPIError(
                "Upstox token verification returned an invalid response"
            )
        if str(data.get("user_id", "")) != self.settings.expected_user_id:
            raise UpstoxAPIError(
                "Upstox token verification returned an unexpected user"
            )

    @staticmethod
    def _json_object(
        response: requests.Response, operation: str
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except requests.JSONDecodeError as error:
            raise UpstoxAPIError(
                f"Upstox {operation} returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise UpstoxAPIError(
                f"Upstox {operation} returned a non-object response"
            )
        return payload

