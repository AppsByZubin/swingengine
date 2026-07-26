"""Atomic persistent storage for the current Upstox access token."""

from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Any


class TokenStateError(RuntimeError):
    """Raised when persisted token state cannot be read or written safely."""


@dataclass(frozen=True)
class TokenState:
    """Persisted authorization state.

    The access token is deliberately excluded from repr output to prevent
    accidental credential disclosure in logs and tracebacks.
    """

    access_token: str = field(default="", repr=False)
    client_id: str = ""
    user_id: str = ""
    token_type: str = ""
    issued_at: int | None = None
    expires_at: int | None = None
    last_request_date: str = ""
    authorization_expiry: int | None = None
    updated_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TokenState":
        def optional_int(name: str) -> int | None:
            value = raw.get(name)
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            return int(value)

        return cls(
            access_token=str(raw.get("access_token", "")),
            client_id=str(raw.get("client_id", "")),
            user_id=str(raw.get("user_id", "")),
            token_type=str(raw.get("token_type", "")),
            issued_at=optional_int("issued_at"),
            expires_at=optional_int("expires_at"),
            last_request_date=str(raw.get("last_request_date", "")),
            authorization_expiry=optional_int("authorization_expiry"),
            updated_at=str(raw.get("updated_at", "")),
        )

    def is_valid(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        return bool(
            self.access_token
            and self.expires_at is not None
            and self.expires_at > int(current.timestamp() * 1000)
        )


class TokenStore:
    """Read and atomically replace token state on a persistent volume."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = RLock()

    def load(self) -> TokenState:
        with self._lock:
            if not self.path.exists():
                return TokenState()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("token state must be a JSON object")
                return TokenState.from_dict(raw)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise TokenStateError(
                    f"Unable to read token state from {self.path}"
                ) from error

    def record_request(
        self, request_date: str, authorization_expiry: int
    ) -> TokenState:
        with self._lock:
            current = self.load()
            updated = replace(
                current,
                last_request_date=request_date,
                authorization_expiry=authorization_expiry,
                updated_at=datetime.now(UTC).isoformat(),
            )
            self._write(updated)
            return updated

    def record_token(
        self,
        *,
        access_token: str,
        client_id: str,
        user_id: str,
        token_type: str,
        issued_at: int,
        expires_at: int,
    ) -> TokenState:
        with self._lock:
            current = self.load()
            updated = replace(
                current,
                access_token=access_token,
                client_id=client_id,
                user_id=user_id,
                token_type=token_type,
                issued_at=issued_at,
                expires_at=expires_at,
                updated_at=datetime.now(UTC).isoformat(),
            )
            self._write(updated)
            return updated

    def _write(self, state: TokenState) -> None:
        parent = self.path.parent
        temporary_path: Path | None = None
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=parent
            )
            temporary_path = Path(temporary_name)
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
        except OSError as error:
            raise TokenStateError(
                f"Unable to write token state to {self.path}"
            ) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

