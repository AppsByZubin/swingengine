import main


def test_project_entry_point_starts_slack_worker(monkeypatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(main, "run_slack", lambda: started.append(True))

    assert main.main() == 0
    assert started == [True]
