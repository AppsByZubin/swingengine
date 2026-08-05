from pathlib import Path

import main


def test_project_entry_point_starts_slack_worker(monkeypatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(main, "run_slack", lambda: started.append(True))

    assert main.main() == 0
    assert started == [True]


def test_container_includes_all_imported_application_packages() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(
        encoding="utf-8"
    )

    for package in ("database", "fundamental", "slack", "tracker", "upstox"):
        assert f"COPY {package} ./{package}" in dockerfile
