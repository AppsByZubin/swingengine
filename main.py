"""Project entry point and future worker orchestrator."""

import logging

from database.config import DatabaseConfigurationError
from slack.app import run as run_slack
from slack.config import ConfigurationError
from tracker.config import TrackerEvaluationConfigurationError
from upstox.assets import AssetConfigurationError
from upstox.config import UpstoxConfigurationError

LOGGER = logging.getLogger(__name__)


def main() -> int:
    try:
        run_slack()
    except (
        ConfigurationError,
        DatabaseConfigurationError,
        UpstoxConfigurationError,
        AssetConfigurationError,
        TrackerEvaluationConfigurationError,
    ) as error:
        print(f"Configuration error: {error}")
        return 2
    except KeyboardInterrupt:
        LOGGER.info("SwingEngine stopped")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
