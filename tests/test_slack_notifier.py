from slack.notifier import SlackTokenNotifier


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def chat_postMessage(self, **kwargs: str) -> None:
        self.calls.append(kwargs)


def test_notifier_posts_private_alert_to_configured_user() -> None:
    client = RecordingClient()
    notifier = SlackTokenNotifier(
        client,  # type: ignore[arg-type]
        user_id="U123",
    )

    notifier.notify("Token invalid.")

    assert client.calls == [
        {
            "channel": "U123",
            "text": ":warning: Token invalid.",
        }
    ]
