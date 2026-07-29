"""Upstox HTTP client for authorization and daily candle retrieval."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from threading import Lock
from typing import Any
from urllib.parse import quote

import requests

from upstox.config import UpstoxSettings


class UpstoxAPIError(RuntimeError):
    """Raised for a sanitized Upstox API failure."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TokenRequest:
    authorization_expiry: int


@dataclass(frozen=True, slots=True)
class DailyCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float


class UpstoxAuthClient:
    """Call the endpoints required by token and tracker workflows."""

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
                f"Upstox token request returned HTTP {response.status_code}",
                response.status_code,
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
                f"Upstox token verification returned HTTP {response.status_code}",
                response.status_code,
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

    def get_daily_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return daily candles through the requested date, including today.

        Upstox separates historical dates from the current trading day, so the
        V3 historical and intraday endpoints are combined and deduplicated.
        """
        if from_date > through_date:
            raise ValueError("from_date cannot be after through_date")
        encoded_key = quote(instrument_key, safe="")
        candles: list[DailyCandle] = []

        historical_to = through_date - timedelta(days=1)
        if from_date <= historical_to:
            historical_url = (
                f"{self.settings.api_base_url}/v3/historical-candle/"
                f"{encoded_key}/days/1/{historical_to.isoformat()}/"
                f"{from_date.isoformat()}"
            )
            historical_payload = self._authorized_get(
                historical_url,
                access_token,
                "historical candle",
            )
            candles.extend(
                self._daily_candles(historical_payload, "historical candle")
            )

        intraday_url = (
            f"{self.settings.api_base_url}/v3/historical-candle/intraday/"
            f"{encoded_key}/days/1"
        )
        intraday_payload = self._authorized_get(
            intraday_url,
            access_token,
            "intraday candle",
        )
        candles.extend(
            candle
            for candle in self._daily_candles(
                intraday_payload, "intraday candle"
            )
            if candle.timestamp.date() == through_date
        )

        by_date = {candle.timestamp.date(): candle for candle in candles}
        return [by_date[trading_date] for trading_date in sorted(by_date)]

    def _authorized_get(
        self,
        url: str,
        access_token: str,
        operation: str,
    ) -> dict[str, Any]:
        try:
            with self._request_lock:
                response = self._session.get(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {access_token}",
                    },
                    timeout=self.settings.request_timeout_seconds,
                )
        except requests.RequestException as error:
            raise UpstoxAPIError(
                f"Upstox {operation} request could not be reached"
            ) from error
        if response.status_code != 200:
            raise UpstoxAPIError(
                f"Upstox {operation} request returned HTTP "
                f"{response.status_code}",
                response.status_code,
            )
        return self._json_object(response, operation)

    @staticmethod
    def _daily_candles(
        payload: dict[str, Any],
        operation: str,
    ) -> list[DailyCandle]:
        data = payload.get("data")
        raw_candles = data.get("candles") if isinstance(data, dict) else None
        if payload.get("status") != "success" or not isinstance(
            raw_candles, list
        ):
            raise UpstoxAPIError(
                f"Upstox {operation} returned an invalid response"
            )

        candles: list[DailyCandle] = []
        try:
            for raw in raw_candles:
                if not isinstance(raw, list) or len(raw) < 7:
                    raise ValueError
                timestamp = datetime.fromisoformat(
                    str(raw[0]).replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None:
                    raise ValueError
                numeric_values = [
                    float(value) for value in raw[1:7]
                ]
                candles.append(
                    DailyCandle(timestamp, *numeric_values)
                )
        except (TypeError, ValueError, OverflowError) as error:
            raise UpstoxAPIError(
                f"Upstox {operation} returned invalid candle data"
            ) from error
        return candles

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
