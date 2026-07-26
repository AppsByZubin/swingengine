from datetime import UTC, datetime, timedelta
import json
import stat

import pytest

from upstox.store import TokenStateError, TokenStore


def test_token_store_atomically_persists_request_and_token(tmp_path) -> None:
    token_path = tmp_path / "state" / "token.json"
    store = TokenStore(token_path)
    now = datetime.now(UTC)

    store.record_request("2026-07-27", int(now.timestamp() * 1000))
    store.record_token(
        access_token="sensitive-token",
        client_id="client",
        user_id="USER1",
        token_type="Bearer",
        issued_at=int(now.timestamp() * 1000),
        expires_at=int((now + timedelta(hours=20)).timestamp() * 1000),
    )

    state = store.load()
    assert state.access_token == "sensitive-token"
    assert state.last_request_date == "2026-07-27"
    assert state.is_valid(now)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert "sensitive-token" not in repr(state)


def test_token_store_rejects_corrupt_state(tmp_path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps(["not", "an", "object"]))

    with pytest.raises(TokenStateError, match="Unable to read token state"):
        TokenStore(token_path).load()

