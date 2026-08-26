"""Upstox HTTP client for authorization, candles, and market quotes."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import logging
from math import isfinite
from threading import Lock
from time import sleep
from typing import Any
from urllib.parse import quote

import requests

from upstox.config import UpstoxSettings

LOGGER = logging.getLogger(__name__)

AUTHORIZED_GET_MAX_ATTEMPTS = 3
AUTHORIZED_GET_BACKOFF_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 60.0
RETRIABLE_HTTP_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
FUNDAMENTAL_ENDPOINTS = frozenset(
    {
        "profile",
        "key-ratios",
        "balance-sheet",
        "income-statement",
        "cash-flow",
        "corporate-actions",
        "share-holdings",
        "competitors",
    }
)


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


@dataclass(frozen=True, slots=True)
class DailyMarketQuote:
    """Latest price and current daily candle for one instrument."""

    instrument_key: str
    last_price: float
    candle: DailyCandle


class UpstoxAuthClient:
    """Call the endpoints required by token and tracker workflows."""

    def __init__(
        self,
        settings: UpstoxSettings,
        session: requests.Session | None = None,
        *,
        sleep_function: Callable[[float], None] = sleep,
    ):
        self.settings = settings
        self._session = session or requests.Session()
        self._sleep = sleep_function
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

    def get_historical_daily_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return historical daily candles without an intraday API call."""
        if from_date > through_date:
            raise ValueError("from_date cannot be after through_date")
        encoded_key = quote(instrument_key, safe="")
        url = (
            f"{self.settings.api_base_url}/v3/historical-candle/"
            f"{encoded_key}/days/1/{through_date.isoformat()}/"
            f"{from_date.isoformat()}"
        )
        payload = self._authorized_get(
            url,
            access_token,
            "historical candle",
        )
        candles = self._daily_candles(payload, "historical candle")
        by_date = {candle.timestamp.date(): candle for candle in candles}
        return [by_date[trading_date] for trading_date in sorted(by_date)]

    def get_hourly_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return hourly candles through the requested date, including today.

        Mirrors ``get_daily_candles``: the V3 historical and intraday
        endpoints are combined and deduplicated, but by full timestamp since
        several hourly bars share a calendar date.
        """
        if from_date > through_date:
            raise ValueError("from_date cannot be after through_date")
        encoded_key = quote(instrument_key, safe="")
        candles: list[DailyCandle] = []

        historical_to = through_date - timedelta(days=1)
        if from_date <= historical_to:
            historical_url = (
                f"{self.settings.api_base_url}/v3/historical-candle/"
                f"{encoded_key}/hours/1/{historical_to.isoformat()}/"
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
            f"{encoded_key}/hours/1"
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

        return _deduplicate_by_timestamp(candles)

    def get_historical_hourly_candles(
        self,
        access_token: str,
        instrument_key: str,
        from_date: date,
        through_date: date,
    ) -> list[DailyCandle]:
        """Return historical hourly candles without an intraday API call."""
        if from_date > through_date:
            raise ValueError("from_date cannot be after through_date")
        encoded_key = quote(instrument_key, safe="")
        url = (
            f"{self.settings.api_base_url}/v3/historical-candle/"
            f"{encoded_key}/hours/1/{through_date.isoformat()}/"
            f"{from_date.isoformat()}"
        )
        payload = self._authorized_get(
            url,
            access_token,
            "historical candle",
        )
        candles = self._daily_candles(payload, "historical candle")
        return _deduplicate_by_timestamp(candles)

    def get_daily_market_quotes(
        self,
        access_token: str,
        instrument_keys: Sequence[str],
    ) -> dict[str, DailyMarketQuote]:
        """Return V3 daily OHLC/LTP snapshots for up to 500 instruments."""
        keys = tuple(str(key).strip() for key in instrument_keys)
        if not keys or any(not key for key in keys):
            raise ValueError("instrument_keys must contain non-empty keys")
        if len(keys) > 500:
            raise ValueError("at most 500 instrument keys can be requested")
        if len(set(keys)) != len(keys):
            raise ValueError("instrument_keys must be unique")

        url = f"{self.settings.api_base_url}/v3/market-quote/ohlc"
        payload = self._authorized_get(
            url,
            access_token,
            "daily market quote",
            params={"instrument_key": ",".join(keys), "interval": "1d"},
        )
        return self._daily_market_quotes(payload)

    def get_fundamental_data(
        self,
        access_token: str,
        isin: str,
        endpoint: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return one documented Upstox company-fundamentals payload."""
        normalized_isin = isin.strip().upper()
        normalized_endpoint = endpoint.strip().casefold()
        if not normalized_isin:
            raise ValueError("isin cannot be empty")
        if normalized_endpoint not in FUNDAMENTAL_ENDPOINTS:
            raise ValueError("unsupported fundamentals endpoint")

        encoded_isin = quote(normalized_isin, safe="")
        url = (
            f"{self.settings.api_base_url}/v2/fundamentals/"
            f"{encoded_isin}/{normalized_endpoint}"
        )
        return self._authorized_get(
            url,
            access_token,
            f"fundamentals {normalized_endpoint}",
            params=params,
        )

    def _authorized_get(
        self,
        url: str,
        access_token: str,
        operation: str,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        request_arguments: dict[str, Any] = {
            "headers": {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            "timeout": self.settings.request_timeout_seconds,
        }
        if params is not None:
            request_arguments["params"] = params

        for attempt in range(1, AUTHORIZED_GET_MAX_ATTEMPTS + 1):
            try:
                with self._request_lock:
                    response = self._session.get(
                        url,
                        **request_arguments,
                    )
            except (requests.ConnectionError, requests.Timeout) as error:
                if attempt < AUTHORIZED_GET_MAX_ATTEMPTS:
                    delay = _retry_delay_seconds(attempt)
                    LOGGER.warning(
                        "Retrying Upstox %s request after transport failure "
                        "attempt=%d/%d delay_seconds=%.2f error=%s url=%s",
                        operation,
                        attempt,
                        AUTHORIZED_GET_MAX_ATTEMPTS,
                        delay,
                        type(error).__name__,
                        url,
                    )
                    self._sleep(delay)
                    continue
                raise UpstoxAPIError(
                    f"Upstox {operation} request could not be reached"
                ) from error
            except requests.RequestException as error:
                raise UpstoxAPIError(
                    f"Upstox {operation} request could not be reached"
                ) from error

            if response.status_code == 200:
                return self._json_object(response, operation)
            if (
                response.status_code in RETRIABLE_HTTP_STATUS_CODES
                and attempt < AUTHORIZED_GET_MAX_ATTEMPTS
            ):
                delay = _retry_delay_seconds(attempt, response)
                LOGGER.warning(
                    "Retrying Upstox %s request after HTTP response "
                    "attempt=%d/%d delay_seconds=%.2f status_code=%d "
                    "url=%s",
                    operation,
                    attempt,
                    AUTHORIZED_GET_MAX_ATTEMPTS,
                    delay,
                    response.status_code,
                    url,
                )
                close_response = getattr(response, "close", None)
                if callable(close_response):
                    close_response()
                self._sleep(delay)
                continue
            raise UpstoxAPIError(
                f"Upstox {operation} request returned HTTP "
                f"{response.status_code}",
                response.status_code,
            )

        raise AssertionError("authorized GET retry loop did not return or raise")

    @staticmethod
    def _daily_market_quotes(
        payload: dict[str, Any],
    ) -> dict[str, DailyMarketQuote]:
        data = payload.get("data")
        if payload.get("status") != "success" or not isinstance(data, dict):
            raise UpstoxAPIError(
                "Upstox daily market quote returned an invalid response"
            )

        quotes: dict[str, DailyMarketQuote] = {}
        try:
            for raw_quote in data.values():
                if not isinstance(raw_quote, dict):
                    raise ValueError
                instrument_key = str(
                    raw_quote.get("instrument_token", "")
                ).strip()
                live_ohlc = raw_quote.get("live_ohlc")
                if not instrument_key or not isinstance(live_ohlc, dict):
                    raise ValueError

                last_price = float(raw_quote["last_price"])
                timestamp_milliseconds = int(live_ohlc["ts"])
                numeric_values = [
                    float(live_ohlc[name])
                    for name in ("open", "high", "low", "close", "volume")
                ]
                if not isfinite(last_price) or not all(
                    isfinite(value) for value in numeric_values
                ):
                    raise ValueError
                if instrument_key in quotes:
                    raise ValueError

                quotes[instrument_key] = DailyMarketQuote(
                    instrument_key=instrument_key,
                    last_price=last_price,
                    candle=DailyCandle(
                        timestamp=datetime.fromtimestamp(
                            timestamp_milliseconds / 1000,
                            tz=UTC,
                        ),
                        open=numeric_values[0],
                        high=numeric_values[1],
                        low=numeric_values[2],
                        close=numeric_values[3],
                        volume=numeric_values[4],
                        open_interest=0.0,
                    ),
                )
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            OverflowError,
        ) as error:
            raise UpstoxAPIError(
                "Upstox daily market quote returned invalid quote data"
            ) from error
        return quotes

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


def _deduplicate_by_timestamp(
    candles: Sequence[DailyCandle],
) -> list[DailyCandle]:
    by_timestamp = {candle.timestamp: candle for candle in candles}
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def _retry_delay_seconds(
    attempt: int,
    response: requests.Response | None = None,
) -> float:
    default_delay = AUTHORIZED_GET_BACKOFF_SECONDS * (2 ** (attempt - 1))
    if response is None:
        return default_delay
    headers = getattr(response, "headers", None)
    retry_after = (
        headers.get("Retry-After")
        if isinstance(headers, Mapping)
        else None
    )
    try:
        parsed_retry_after = float(retry_after)
    except (TypeError, ValueError):
        return default_delay
    if not isfinite(parsed_retry_after) or parsed_retry_after < 0:
        return default_delay
    return min(parsed_retry_after, MAX_RETRY_AFTER_SECONDS)
