from slack.commands import CommandRouter, build_router, ephemeral


def test_empty_command_shows_help() -> None:
    response = build_router().dispatch("")

    assert response["response_type"] == "ephemeral"
    assert "/swingengine help" in response["text"]


def test_command_names_are_case_insensitive() -> None:
    assert build_router().dispatch("  PiNg  ") == ephemeral("pong")


def test_any_whitespace_can_separate_command_and_arguments() -> None:
    router = CommandRouter()
    router.register("help", lambda arguments: ephemeral("help"))
    router.register("echo", lambda arguments: ephemeral(arguments))

    assert router.dispatch("echo\tone two") == ephemeral("one two")


def test_status_command_reports_running() -> None:
    assert "running" in build_router().dispatch("status")["text"]


def test_unknown_command_suggests_help() -> None:
    response = build_router().dispatch("buy NOW")

    assert "Unknown command `buy`" in response["text"]
    assert "/swingengine help" in response["text"]


def test_router_passes_arguments_to_handler() -> None:
    router = CommandRouter()
    router.register("help", lambda arguments: ephemeral("help"))
    router.register("echo", lambda arguments: ephemeral(arguments))

    assert router.dispatch("echo one two") == ephemeral("one two")


def test_router_rejects_invalid_command_names() -> None:
    router = CommandRouter()

    try:
        router.register("two words", lambda arguments: ephemeral(arguments))
    except ValueError as error:
        assert "one non-empty word" in str(error)
    else:
        raise AssertionError("Expected ValueError")
