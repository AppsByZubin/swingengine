"""Token-rotation orchestration and webhook payload validation."""

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from upstox.client import UpstoxAPIError, UpstoxAuthClient
from upstox.config import UpstoxSettings
from upstox.store import TokenState, TokenStateError, TokenStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    message: str
    status_code: int = 200


class TokenRotationService:
    """Coordinate token requests, validation, persistence, and status."""

    def __init__(
        self,
        settings: UpstoxSettings,
        store: TokenStore,
        client: UpstoxAuthClient,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self._request_lock = Lock()

    def status_message(self, now: datetime | None = None) -> str:
        if not self.settings.enabled:
            return "Upstox token management is disabled."
        if self.settings.credential_errors:
            return "Upstox token management is misconfigured: " + "; ".join(
                self.settings.credential_errors
            )

        current = now or datetime.now(UTC)
        try:
            state = self.store.load()
        except TokenStateError:
            return "Upstox token state cannot be read."

        local_timezone = ZoneInfo(self.settings.timezone_name)
        if state.is_valid(current):
            message = "Upstox trading token is valid"
            if state.expires_at is not None:
                expiry = datetime.fromtimestamp(
                    state.expires_at / 1000, tz=UTC
                ).astimezone(local_timezone)
                message += f" until {expiry:%Y-%m-%d %H:%M:%S %Z}"
            if state.last_verified_at is not None:
                verified = datetime.fromtimestamp(
                    state.last_verified_at / 1000, tz=UTC
                ).astimezone(local_timezone)
                message += f"; last checked {verified:%Y-%m-%d %H:%M:%S %Z}"
            return message + "."

        if state.validation_status == "invalid":
            return (
                "Upstox trading token is invalid. Use "
                "`/swingengine auth set <token>`."
            )
        if state.access_token:
            return (
                "Upstox trading token is expired or unchecked. Use "
                "`/swingengine auth set <token>`."
            )
        return (
            "No Upstox trading token is stored. Use "
            "`/swingengine auth set <token>`."
        )

    def set_token_message(self, access_token: str) -> str:
        return self.set_token(access_token).message

    def set_token(
        self,
        access_token: str,
        *,
        now: datetime | None = None,
    ) -> OperationResult:
        if not self.settings.enabled:
            return OperationResult(False, "Upstox token management is disabled.", 503)
        if self.settings.credential_errors:
            return OperationResult(
                False,
                "Upstox token management is misconfigured: "
                + "; ".join(self.settings.credential_errors),
                503,
            )

        token = access_token.strip()
        if not token or any(char.isspace() for char in token):
            return OperationResult(False, "Provide one non-empty access token.", 400)
        if len(token) > 16_384:
            return OperationResult(False, "Access token is too large.", 413)

        current = now or datetime.now(UTC)
        current_milliseconds = int(current.timestamp() * 1000)
        try:
            self.client.verify_access_token(token)
            issued_at, expires_at = _jwt_timestamps(token)
            if expires_at is not None and expires_at <= current_milliseconds:
                return OperationResult(False, "Upstox token is already expired.", 400)
            self.store.record_token(
                access_token=token,
                client_id=self.settings.api_key,
                user_id=self.settings.expected_user_id,
                token_type="Bearer",
                issued_at=issued_at or current_milliseconds,
                expires_at=expires_at,
                verified_at=current_milliseconds,
            )
        except UpstoxAPIError as error:
            LOGGER.warning("Rejected a manually supplied Upstox token: %s", error)
            return OperationResult(False, "Upstox rejected the supplied token.", 400)
        except TokenStateError as error:
            LOGGER.error("Could not persist the supplied Upstox token: %s", error)
            return OperationResult(False, "Could not persist access token.", 500)

        LOGGER.info("Validated and stored a manually supplied Upstox token")
        return OperationResult(True, self.status_message(current))

    def validate_current_token(
        self, *, now: datetime | None = None
    ) -> OperationResult:
        if not self.settings.enabled:
            return OperationResult(False, "Upstox token management is disabled.", 503)
        if self.settings.credential_errors:
            return OperationResult(
                False,
                "Upstox token management is misconfigured: "
                + "; ".join(self.settings.credential_errors),
                503,
            )

        current = now or datetime.now(UTC)
        checked_at = int(current.timestamp() * 1000)
        try:
            state = self.store.load()
        except TokenStateError:
            return OperationResult(False, "Upstox token state cannot be read.", 500)
        if not state.access_token:
            return OperationResult(
                False,
                "No Upstox token is stored. Use "
                "`/swingengine auth set <token>`.",
                404,
            )

        try:
            self.client.verify_access_token(state.access_token)
            _, expires_at = _jwt_timestamps(state.access_token)
            self.store.record_validation(
                valid=True,
                verified_at=checked_at,
                expires_at=expires_at,
            )
        except UpstoxAPIError as error:
            if error.status_code in {401, 403}:
                try:
                    self.store.record_validation(
                        valid=False,
                        verified_at=checked_at,
                    )
                except TokenStateError:
                    pass
                return OperationResult(
                    False,
                    "Upstox rejected the stored token. Use "
                    "`/swingengine auth set <token>`.",
                    401,
                )
            return OperationResult(
                False,
                "SwingEngine could not verify the Upstox token; Upstox "
                "may be temporarily unavailable.",
                502,
            )
        except TokenStateError:
            return OperationResult(
                False, "Could not update Upstox token state.", 500
            )

        return OperationResult(True, self.status_message(current))

    def request_token_message(self, force: bool = True) -> str:
        return self.request_token(force=force).message

    def request_token(
        self,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> OperationResult:
        if not self.settings.rotation_enabled:
            return OperationResult(False, "Upstox token rotation is disabled.", 503)
        if self.settings.credential_errors:
            return OperationResult(
                False,
                "Upstox token rotation is misconfigured: "
                + "; ".join(self.settings.credential_errors),
                503,
            )

        current = now or datetime.now(UTC)
        local_timezone = ZoneInfo(self.settings.timezone_name)
        request_date = current.astimezone(local_timezone).date().isoformat()

        with self._request_lock:
            try:
                state = self.store.load()
                if not force and state.last_request_date == request_date:
                    return OperationResult(
                        True,
                        "Upstox token approval has already been requested today.",
                    )

                request = self.client.request_access_token()
                self.store.record_request(
                    request_date, request.authorization_expiry
                )
            except (TokenStateError, UpstoxAPIError) as error:
                LOGGER.warning("Upstox token request failed: %s", error)
                return OperationResult(False, str(error), 502)

        expiry = datetime.fromtimestamp(
            request.authorization_expiry / 1000, tz=UTC
        ).astimezone(local_timezone)
        LOGGER.info(
            "Requested Upstox token approval request_date=%s "
            "authorization_expiry=%s",
            request_date,
            expiry.isoformat(),
        )
        return OperationResult(
            True,
            "Upstox approval requested. Approve it in the Upstox app or "
            f"WhatsApp before {expiry:%Y-%m-%d %H:%M:%S %Z}.",
        )

    def accept_webhook(
        self,
        payload: Any,
        *,
        now: datetime | None = None,
    ) -> OperationResult:
        if not self.settings.rotation_enabled or self.settings.credential_errors:
            return OperationResult(
                False, "Upstox token rotation is unavailable.", 503
            )
        if not isinstance(payload, dict):
            return OperationResult(False, "Expected a JSON object.", 400)

        required_strings = (
            "client_id",
            "user_id",
            "access_token",
            "token_type",
            "message_type",
        )
        values: dict[str, str] = {}
        for name in required_strings:
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip():
                return OperationResult(
                    False, f"Missing or invalid {name}.", 400
                )
            values[name] = value.strip()

        if values["client_id"] != self.settings.api_key:
            return OperationResult(False, "Unexpected client_id.", 403)
        if values["user_id"] != self.settings.expected_user_id:
            return OperationResult(False, "Unexpected user_id.", 403)
        if values["token_type"].casefold() != "bearer":
            return OperationResult(False, "Unexpected token_type.", 400)
        if values["message_type"] != "access_token":
            return OperationResult(False, "Unexpected message_type.", 400)
        if len(values["access_token"]) > 16_384:
            return OperationResult(False, "access_token is too large.", 413)

        try:
            issued_at = _payload_milliseconds(payload, "issued_at")
            expires_at = _payload_milliseconds(payload, "expires_at")
        except ValueError as error:
            return OperationResult(False, str(error), 400)

        current = now or datetime.now(UTC)
        current_milliseconds = int(current.timestamp() * 1000)
        if expires_at <= current_milliseconds:
            return OperationResult(False, "Received token is already expired.", 400)
        if issued_at >= expires_at:
            return OperationResult(
                False, "issued_at must be earlier than expires_at.", 400
            )
        if issued_at > current_milliseconds + 300_000:
            return OperationResult(False, "issued_at is too far in the future.", 400)

        try:
            if self.settings.verify_webhook_token:
                self.client.verify_access_token(values["access_token"])
            self.store.record_token(
                access_token=values["access_token"],
                client_id=values["client_id"],
                user_id=values["user_id"],
                token_type="Bearer",
                issued_at=issued_at,
                expires_at=expires_at,
                verified_at=current_milliseconds,
            )
        except UpstoxAPIError as error:
            LOGGER.warning("Rejected an unverifiable Upstox token: %s", error)
            return OperationResult(False, str(error), 502)
        except TokenStateError as error:
            LOGGER.error("Could not persist the approved Upstox token: %s", error)
            return OperationResult(False, "Could not persist access token.", 500)

        LOGGER.info(
            "Stored approved Upstox token user_id=%s expires_at=%s",
            values["user_id"],
            datetime.fromtimestamp(expires_at / 1000, tz=UTC).isoformat(),
        )
        return OperationResult(True, "Access token stored.")

    def current_token(self, now: datetime | None = None) -> str | None:
        try:
            state: TokenState = self.store.load()
        except TokenStateError:
            return None
        return state.access_token if state.is_valid(now) else None


def _payload_milliseconds(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool):
        raise ValueError(f"Missing or invalid {name}.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Missing or invalid {name}.") from error
    if parsed <= 0:
        raise ValueError(f"Missing or invalid {name}.")
    return parsed


def _jwt_timestamps(access_token: str) -> tuple[int | None, int | None]:
    """Read already-verified JWT timestamps for display and local expiry."""
    parts = access_token.split(".")
    if len(parts) != 3:
        return None, None
    try:
        encoded_payload = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(encoded_payload).decode("utf-8")
        )
        if not isinstance(payload, dict):
            return None, None
        issued_at = _seconds_to_milliseconds(payload.get("iat"))
        expires_at = _seconds_to_milliseconds(payload.get("exp"))
        return issued_at, expires_at
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        binascii.Error,
    ):
        return None, None


def _seconds_to_milliseconds(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return seconds * 1000 if seconds > 0 else None
