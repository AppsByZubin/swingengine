"""Atomic persistent storage for the current Zerodha access token."""

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
    validation_status: str = "unchecked"
    last_verified_at: int | None = None
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

        access_token = str(raw.get("access_token", ""))
        validation_status = str(raw.get("validation_status", "")).strip()
        if not validation_status:
            validation_status = "valid" if access_token else "unchecked"

        return cls(
            access_token=access_token,
            validation_status=validation_status,
            last_verified_at=optional_int("last_verified_at"),
            updated_at=str(raw.get("updated_at", "")),
        )

    def is_valid(self) -> bool:
        return bool(self.access_token and self.validation_status == "valid")


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

    def record_token(
        self,
        *,
        access_token: str,
        verified_at: int | None = None,
    ) -> TokenState:
        with self._lock:
            current = self.load()
            updated = replace(
                current,
                access_token=access_token,
                validation_status="valid",
                last_verified_at=verified_at,
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
