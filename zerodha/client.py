"""Kite Connect HTTP client for access-token verification."""

from typing import Any

import requests

from zerodha.config import ZerodhaSettings


class KiteAPIError(RuntimeError):
    """Raised for a sanitized Kite Connect API failure."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class KiteAuthClient:
    """Verify a Kite Connect access token against the profile endpoint."""

    def __init__(
        self,
        settings: ZerodhaSettings,
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self._session = session or requests.Session()

    def verify_access_token(self, access_token: str) -> None:
        url = f"{self.settings.api_base_url}/user/profile"
        try:
            response = self._session.get(
                url,
                headers={
                    "Accept": "application/json",
                    "X-Kite-Version": "3",
                    "Authorization": (
                        f"token {self.settings.api_key}:{access_token}"
                    ),
                },
                timeout=self.settings.request_timeout_seconds,
            )
        except requests.RequestException as error:
            raise KiteAPIError(
                "Kite token verification could not be reached"
            ) from error

        if response.status_code != 200:
            raise KiteAPIError(
                f"Kite token verification returned HTTP {response.status_code}",
                response.status_code,
            )
        payload = self._json_object(response)
        data: Any = payload.get("data")
        if payload.get("status") != "success" or not isinstance(data, dict):
            raise KiteAPIError(
                "Kite token verification returned an invalid response"
            )
        if str(data.get("user_id", "")) != self.settings.expected_user_id:
            raise KiteAPIError(
                "Kite token verification returned an unexpected user"
            )

    @staticmethod
    def _json_object(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except requests.JSONDecodeError as error:
            raise KiteAPIError(
                "Kite token verification returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise KiteAPIError(
                "Kite token verification returned a non-object response"
            )
        return payload
