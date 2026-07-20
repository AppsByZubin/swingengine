import pytest

from slack.config import ConfigurationError, Settings


def test_settings_load_valid_environment() -> None:
    settings = Settings.from_env(
        {
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_APP_TOKEN": "xapp-test",
            "SLACK_COMMAND": "/swing",
            "LOG_LEVEL": "debug",
        }
    )

    assert settings.slash_command == "/swing"
    assert settings.log_level == "DEBUG"


def test_settings_report_all_missing_tokens() -> None:
    with pytest.raises(ConfigurationError) as exception:
        Settings.from_env({})

    message = str(exception.value)
    assert "SLACK_BOT_TOKEN is required" in message
    assert "SLACK_APP_TOKEN is required" in message


@pytest.mark.parametrize("slash_command", ["swingengine", "/two words", ""])
def test_settings_reject_invalid_slash_command(slash_command: str) -> None:
    with pytest.raises(ConfigurationError, match="SLACK_COMMAND"):
        Settings.from_env(
            {
                "SLACK_BOT_TOKEN": "xoxb-test",
                "SLACK_APP_TOKEN": "xapp-test",
                "SLACK_COMMAND": slash_command,
            }
        )
