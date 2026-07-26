"""Proactive Slack alerts for invalid Upstox authorization."""

import logging
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError, SlackClientError

LOGGER = logging.getLogger(__name__)


class SlackClient(Protocol):
    def chat_postMessage(self, **kwargs: Any) -> Any:
        """Post a persistent private message to a Slack user."""


class SlackTokenNotifier:
    def __init__(
        self,
        client: SlackClient,
        user_id: str,
    ):
        self.client = client
        self.user_id = user_id

    def notify(self, message: str) -> None:
        if not self.user_id:
            LOGGER.error(
                "Cannot send Upstox token alert: SLACK_ALERT_USER_ID must be "
                "configured"
            )
            return
        try:
            self.client.chat_postMessage(
                channel=self.user_id,
                text=f":warning: {message}",
            )
        except SlackApiError as error:
            error_code = str(error.response.get("error", "unknown_error"))
            LOGGER.error("Could not send Slack token alert: %s", error_code)
        except SlackClientError:
            LOGGER.exception("Could not send Slack token alert")
