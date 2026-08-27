from datetime import UTC, datetime
import json
import stat

import pytest

from zerodha.store import TokenStateError, TokenStore


def test_token_store_atomically_persists_token(tmp_path) -> None:
    token_path = tmp_path / "state" / "token.json"
    store = TokenStore(token_path)
    now = datetime.now(UTC)

    store.record_token(
        access_token="sensitive-token",
        verified_at=int(now.timestamp() * 1000),
    )

    state = store.load()
    assert state.access_token == "sensitive-token"
    assert state.is_valid()
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert "sensitive-token" not in repr(state)


def test_token_store_rejects_corrupt_state(tmp_path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps(["not", "an", "object"]))

    with pytest.raises(TokenStateError, match="Unable to read token state"):
        TokenStore(token_path).load()
