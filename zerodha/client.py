"""Kite Connect HTTP client for access-token verification and order/GTT
placement.

Kite Connect's write endpoints (orders, GTT) take form-encoded bodies, not
JSON, unlike Upstox's API — see ``_request`` below.
"""

from dataclasses import dataclass
import json
import logging
from typing import Any
from urllib.parse import quote

import requests

from zerodha.config import ZerodhaSettings

LOGGER = logging.getLogger(__name__)


class KiteAPIError(RuntimeError):
    """Raised for a sanitized Kite Connect API failure."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class KiteOrder:
    """One entry from Kite's order book (``GET /orders``)."""

    order_id: str
    status: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    filled_quantity: int
    average_price: float


@dataclass(frozen=True, slots=True)
class KiteGtt:
    """One GTT trigger (``GET /gtt/triggers``).

    ``stoploss_order_id``/``target_order_id`` are only populated once Kite
    has triggered the GTT and placed the corresponding leg order.
    """

    trigger_id: str
    status: str
    stoploss_order_id: str | None
    target_order_id: str | None


class KiteAuthClient:
    """Verify tokens, place/poll orders, and place/poll GTTs via Kite
    Connect."""

    def __init__(
        self,
        settings: ZerodhaSettings,
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self._session = session or requests.Session()

    def place_limit_order(
        self,
        access_token: str,
        *,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        price: float,
        product: str,
    ) -> str:
        """Place a regular limit order and return its broker order_id."""
        payload = self._request(
            "POST",
            f"{self.settings.api_base_url}/orders/regular",
            access_token,
            "order placement",
            data={
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "transaction_type": transaction_type,
                "order_type": "LIMIT",
                "quantity": str(quantity),
                "product": product,
                "price": f"{price:.2f}",
                "validity": "DAY",
            },
        )
        data = payload.get("data")
        order_id = data.get("order_id") if isinstance(data, dict) else None
        if not isinstance(order_id, str) or not order_id:
            raise KiteAPIError("Kite order placement returned no order_id")
        LOGGER.info(
            "Kite limit order placed tradingsymbol=%r order_id=%s",
            tradingsymbol,
            order_id,
        )
        return order_id

    def cancel_order(self, access_token: str, order_id: str) -> None:
        """Cancel a regular order by its broker order_id."""
        self._request(
            "DELETE",
            f"{self.settings.api_base_url}/orders/regular/"
            f"{quote(order_id, safe='')}",
            access_token,
            "order cancellation",
        )
        LOGGER.info("Kite order cancelled order_id=%s", order_id)

    def get_orders(self, access_token: str) -> list[KiteOrder]:
        """Return the current status of every order placed today."""
        payload = self._request(
            "GET",
            f"{self.settings.api_base_url}/orders",
            access_token,
            "order list",
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise KiteAPIError("Kite order list returned an invalid response")

        orders: list[KiteOrder] = []
        try:
            for raw in data:
                if not isinstance(raw, dict):
                    raise ValueError
                orders.append(
                    KiteOrder(
                        order_id=str(raw["order_id"]),
                        status=str(raw.get("status", "")).upper(),
                        tradingsymbol=str(raw.get("tradingsymbol", "")),
                        transaction_type=str(
                            raw.get("transaction_type", "")
                        ).upper(),
                        quantity=int(raw.get("quantity") or 0),
                        filled_quantity=int(raw.get("filled_quantity") or 0),
                        average_price=float(raw.get("average_price") or 0.0),
                    )
                )
        except (KeyError, TypeError, ValueError) as error:
            raise KiteAPIError(
                "Kite order list returned invalid order data"
            ) from error
        return orders

    def place_gtt(
        self,
        access_token: str,
        *,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        last_price: float,
        target_price: float,
        stoploss_price: float,
        product: str,
    ) -> str:
        """Place a two-leg (target + stoploss) GTT and return its
        trigger_id."""
        condition = {
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "trigger_values": [stoploss_price, target_price],
            "last_price": last_price,
        }
        orders = [
            {
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "order_type": "LIMIT",
                "product": product,
                "price": stoploss_price,
            },
            {
                "exchange": exchange,
                "tradingsymbol": tradingsymbol,
                "transaction_type": "SELL",
                "quantity": quantity,
                "order_type": "LIMIT",
                "product": product,
                "price": target_price,
            },
        ]
        payload = self._request(
            "POST",
            f"{self.settings.api_base_url}/gtt/triggers",
            access_token,
            "GTT placement",
            data={
                "type": "two-leg",
                "condition": json.dumps(condition),
                "orders": json.dumps(orders),
            },
        )
        data = payload.get("data")
        trigger_id = data.get("trigger_id") if isinstance(data, dict) else None
        if trigger_id is None:
            raise KiteAPIError("Kite GTT placement returned no trigger_id")
        LOGGER.info(
            "Kite GTT placed tradingsymbol=%r trigger_id=%s",
            tradingsymbol,
            trigger_id,
        )
        return str(trigger_id)

    def get_gtts(self, access_token: str) -> list[KiteGtt]:
        """Return the current status of every GTT trigger."""
        payload = self._request(
            "GET",
            f"{self.settings.api_base_url}/gtt/triggers",
            access_token,
            "GTT list",
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise KiteAPIError("Kite GTT list returned an invalid response")

        gtts: list[KiteGtt] = []
        try:
            for raw in data:
                if not isinstance(raw, dict):
                    raise ValueError
                leg_order_ids: list[str | None] = [None, None]
                legs = raw.get("orders")
                if isinstance(legs, list):
                    for index, leg in enumerate(legs[:2]):
                        if not isinstance(leg, dict):
                            continue
                        result = leg.get("result")
                        order_result = (
                            result.get("order_result")
                            if isinstance(result, dict)
                            else None
                        )
                        order_id = (
                            order_result.get("order_id")
                            if isinstance(order_result, dict)
                            else None
                        )
                        leg_order_ids[index] = (
                            str(order_id) if order_id else None
                        )
                gtts.append(
                    KiteGtt(
                        trigger_id=str(raw["id"]),
                        status=str(raw.get("status", "")).casefold(),
                        stoploss_order_id=leg_order_ids[0],
                        target_order_id=leg_order_ids[1],
                    )
                )
        except (KeyError, TypeError, ValueError) as error:
            raise KiteAPIError(
                "Kite GTT list returned invalid trigger data"
            ) from error
        return gtts

    def _request(
        self,
        method: str,
        url: str,
        access_token: str,
        operation: str,
        data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        LOGGER.info(
            "Kite %s request method=%s url=%s data=%s",
            operation,
            method,
            url,
            data,
        )
        try:
            response = self._session.request(
                method,
                url,
                headers={
                    "Accept": "application/json",
                    "X-Kite-Version": "3",
                    "Authorization": (
                        f"token {self.settings.api_key}:{access_token}"
                    ),
                },
                data=data,
                timeout=self.settings.request_timeout_seconds,
            )
        except requests.RequestException as error:
            LOGGER.warning(
                "Kite %s request failed transport error=%s url=%s",
                operation,
                type(error).__name__,
                url,
            )
            raise KiteAPIError(f"Kite {operation} could not be reached") from error

        if response.status_code not in (200, 201):
            error_type, error_message = self._error_detail(response)
            LOGGER.warning(
                "Kite %s request failed status_code=%d error_type=%s "
                "message=%r url=%s",
                operation,
                response.status_code,
                error_type,
                error_message,
                url,
            )
            detail = (
                f" ({error_type}: {error_message})"
                if error_type or error_message
                else ""
            )
            raise KiteAPIError(
                f"Kite {operation} returned HTTP {response.status_code}{detail}",
                response.status_code,
            )
        payload = self._json_object(response, operation)
        if payload.get("status") != "success":
            LOGGER.warning(
                "Kite %s request returned an unsuccessful response url=%s",
                operation,
                url,
            )
            raise KiteAPIError(f"Kite {operation} returned an unsuccessful response")
        LOGGER.info(
            "Kite %s request succeeded status_code=%d url=%s",
            operation,
            response.status_code,
            url,
        )
        return payload

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
            LOGGER.warning(
                "Kite token verification failed transport error=%s",
                type(error).__name__,
            )
            raise KiteAPIError(
                "Kite token verification could not be reached"
            ) from error

        if response.status_code != 200:
            LOGGER.warning(
                "Kite token verification failed status_code=%d",
                response.status_code,
            )
            raise KiteAPIError(
                f"Kite token verification returned HTTP {response.status_code}",
                response.status_code,
            )
        payload = self._json_object(response, "token verification")
        data: Any = payload.get("data")
        if payload.get("status") != "success" or not isinstance(data, dict):
            LOGGER.warning("Kite token verification returned an invalid response")
            raise KiteAPIError(
                "Kite token verification returned an invalid response"
            )
        if str(data.get("user_id", "")) != self.settings.expected_user_id:
            LOGGER.warning(
                "Kite token verification returned an unexpected user_id=%r",
                data.get("user_id"),
            )
            raise KiteAPIError(
                "Kite token verification returned an unexpected user"
            )
        LOGGER.info("Kite token verification succeeded")

    @staticmethod
    def _json_object(
        response: requests.Response, operation: str
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except requests.JSONDecodeError as error:
            raise KiteAPIError(f"Kite {operation} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise KiteAPIError(
                f"Kite {operation} returned a non-object response"
            )
        return payload

    @staticmethod
    def _error_detail(
        response: requests.Response,
    ) -> tuple[str | None, str | None]:
        """Best-effort extraction of Kite's ``error_type``/``message`` from
        an error response body, e.g. ``TokenException`` on an expired or
        invalid access token, or ``PermissionException`` when the API key
        is not subscribed for order placement."""
        try:
            payload = response.json()
        except requests.JSONDecodeError:
            return None, None
        if not isinstance(payload, dict):
            return None, None
        error_type = payload.get("error_type")
        message = payload.get("message")
        return (
            str(error_type) if error_type else None,
            str(message) if message else None,
        )
