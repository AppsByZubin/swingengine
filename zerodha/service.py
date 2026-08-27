"""Manual Zerodha (Kite Connect) access-token validation and storage."""

from dataclasses import dataclass
from datetime import UTC, datetime
import logging

from zerodha.client import KiteAPIError, KiteAuthClient
from zerodha.config import ZerodhaSettings
from zerodha.store import TokenStateError, TokenStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    message: str


class ZerodhaTokenService:
    """Validate, persist, and describe a manually supplied Kite token."""

    def __init__(
        self,
        settings: ZerodhaSettings,
        store: TokenStore,
        client: KiteAuthClient,
    ):
        self.settings = settings
        self.store = store
        self.client = client

    def status_message(self) -> str:
        if not self.settings.enabled:
            return "Zerodha token management is disabled."
        if self.settings.credential_errors:
            return "Zerodha token management is misconfigured: " + "; ".join(
                self.settings.credential_errors
            )

        try:
            state = self.store.load()
        except TokenStateError:
            return "Zerodha token state cannot be read."

        if state.is_valid():
            message = "Zerodha trading token is valid"
            if state.last_verified_at is not None:
                verified = datetime.fromtimestamp(
                    state.last_verified_at / 1000, tz=UTC
                )
                message += f"; last checked {verified:%Y-%m-%d %H:%M:%S %Z}"
            return message + "."

        if state.validation_status == "invalid":
            return (
                "Zerodha trading token is invalid. Use "
                "`/swingengine auth set zerodha <token>`."
            )
        return (
            "No Zerodha trading token is stored. Use "
            "`/swingengine auth set zerodha <token>`."
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
            return OperationResult(
                False, "Zerodha token management is disabled."
            )
        if self.settings.credential_errors:
            return OperationResult(
                False,
                "Zerodha token management is misconfigured: "
                + "; ".join(self.settings.credential_errors),
            )

        token = access_token.strip()
        if not token or any(char.isspace() for char in token):
            return OperationResult(False, "Provide one non-empty access token.")
        if len(token) > 16_384:
            return OperationResult(False, "Access token is too large.")

        current = now or datetime.now(UTC)
        current_milliseconds = int(current.timestamp() * 1000)
        try:
            self.client.verify_access_token(token)
            self.store.record_token(
                access_token=token, verified_at=current_milliseconds
            )
        except KiteAPIError as error:
            LOGGER.warning("Rejected a manually supplied Zerodha token: %s", error)
            return OperationResult(False, "Zerodha rejected the supplied token.")
        except TokenStateError as error:
            LOGGER.error(
                "Could not persist the supplied Zerodha token: %s", error
            )
            return OperationResult(False, "Could not persist access token.")

        LOGGER.info("Validated and stored a manually supplied Zerodha token")
        return OperationResult(True, self.status_message())

    def current_token(self) -> str | None:
        try:
            state = self.store.load()
        except TokenStateError:
            return None
        return state.access_token if state.is_valid() else None
