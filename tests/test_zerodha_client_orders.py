from typing import Any

import pytest

from zerodha.client import KiteAPIError, KiteAuthClient
from zerodha.config import ZerodhaSettings


class FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        data: dict[str, str] | None,
        timeout: int,
    ) -> FakeResponse:
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "data": data}
        )
        return self._responses.pop(0)


def settings() -> ZerodhaSettings:
    return ZerodhaSettings.from_env(
        {
            "ZERODHA_TOKEN_MANAGEMENT_ENABLED": "true",
            "ZERODHA_API_KEY": "kite-key",
            "ZERODHA_EXPECTED_USER_ID": "ZD1234",
            "ZERODHA_TOKEN_FILE": "/tmp/zerodha-token.json",
        }
    )


def test_place_limit_order_returns_order_id() -> None:
    session = FakeSession(
        [FakeResponse(200, {"status": "success", "data": {"order_id": "OID1"}})]
    )
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    order_id = client.place_limit_order(
        "token",
        exchange="NSE",
        tradingsymbol="SUNPHARMA",
        transaction_type="BUY",
        quantity=5,
        price=345.0,
        product="CNC",
    )

    assert order_id == "OID1"
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["data"]["transaction_type"] == "BUY"
    assert call["data"]["price"] == "345.00"
    assert call["headers"]["Authorization"] == "token kite-key:token"


def test_place_limit_order_rejects_missing_order_id() -> None:
    session = FakeSession(
        [FakeResponse(200, {"status": "success", "data": {}})]
    )
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    with pytest.raises(KiteAPIError, match="no order_id"):
        client.place_limit_order(
            "token",
            exchange="NSE",
            tradingsymbol="SUNPHARMA",
            transaction_type="BUY",
            quantity=5,
            price=345.0,
            product="CNC",
        )


def test_cancel_order_sends_delete() -> None:
    session = FakeSession(
        [FakeResponse(200, {"status": "success", "data": {"order_id": "OID1"}})]
    )
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    client.cancel_order("token", "OID1")

    assert session.calls[0]["method"] == "DELETE"
    assert session.calls[0]["url"].endswith("/orders/regular/OID1")


def test_get_orders_parses_order_book() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "status": "success",
                    "data": [
                        {
                            "order_id": "OID1",
                            "status": "complete",
                            "tradingsymbol": "SUNPHARMA",
                            "transaction_type": "buy",
                            "quantity": 5,
                            "filled_quantity": 5,
                            "average_price": 346.5,
                        }
                    ],
                },
            )
        ]
    )
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    orders = client.get_orders("token")

    assert len(orders) == 1
    assert orders[0].order_id == "OID1"
    assert orders[0].status == "COMPLETE"
    assert orders[0].average_price == 346.5


def test_get_orders_rejects_invalid_data() -> None:
    session = FakeSession(
        [FakeResponse(200, {"status": "success", "data": "not-a-list"})]
    )
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    with pytest.raises(KiteAPIError, match="invalid response"):
        client.get_orders("token")


def test_place_gtt_returns_trigger_id() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200, {"status": "success", "data": {"trigger_id": 555}}
            )
        ]
    )
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    trigger_id = client.place_gtt(
        "token",
        exchange="NSE",
        tradingsymbol="SUNPHARMA",
        quantity=5,
        last_price=350.0,
        target_price=360.0,
        stoploss_price=340.0,
        product="CNC",
    )

    assert trigger_id == "555"
    call = session.calls[0]
    assert call["data"]["type"] == "two-leg"
    assert "trigger_values" in call["data"]["condition"]


def test_get_gtts_parses_triggered_leg_order_ids() -> None:
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "status": "success",
                    "data": [
                        {
                            "id": 555,
                            "status": "triggered",
                            "orders": [
                                {
                                    "result": {
                                        "order_result": {
                                            "status": "success",
                                            "order_id": "SL1",
                                            "rejection_reason": "",
                                        }
                                    }
                                },
                                {
                                    "result": {
                                        "order_result": {
                                            "status": "success",
                                            "order_id": "TGT1",
                                            "rejection_reason": "",
                                        }
                                    }
                                },
                            ],
                        }
                    ],
                },
            )
        ]
    )
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    gtts = client.get_gtts("token")

    assert len(gtts) == 1
    assert gtts[0].trigger_id == "555"
    assert gtts[0].status == "triggered"
    assert gtts[0].stoploss_order_id == "SL1"
    assert gtts[0].target_order_id == "TGT1"


def test_request_raises_on_non_success_status() -> None:
    session = FakeSession([FakeResponse(500, {"status": "error"})])
    client = KiteAuthClient(settings(), session)  # type: ignore[arg-type]

    with pytest.raises(KiteAPIError) as excinfo:
        client.get_orders("token")
    assert excinfo.value.status_code == 500
