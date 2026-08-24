import pytest

from fundamental.analyzer import (
    EPS_HIGH_PRIORITY_THRESHOLD,
    FundamentalAnalyzer,
    eps_score,
)


def _analyzer_with_ratios(ratios: list[dict]) -> FundamentalAnalyzer:
    analyzer = FundamentalAnalyzer.from_payloads(
        {"key_ratios": {"status": "success", "data": ratios}}, 70.0
    )
    analyzer._load()
    return analyzer


def test_eps_crosses_into_good_territory_at_the_high_priority_threshold() -> None:
    assert eps_score(EPS_HIGH_PRIORITY_THRESHOLD) == pytest.approx(70.0)
    assert eps_score(25) > eps_score(EPS_HIGH_PRIORITY_THRESHOLD) > eps_score(10)


def test_category_weights_make_profitability_high_priority() -> None:
    analyzer = _analyzer_with_ratios([])
    ratios = analyzer.ratio_map()
    weights = {
        category.name: category.weight
        for category in [
            analyzer.analyze_valuation(ratios),
            analyzer.analyze_profitability(ratios),
            analyzer.analyze_growth(),
            analyzer.analyze_financial_health(ratios),
            analyzer.analyze_cash_flow(),
            analyzer.analyze_shareholder_returns(),
        ]
    }
    assert weights == {
        "Valuation": 20.0,
        "Profitability": 30.0,
        "Growth": 15.0,
        "Financial health": 15.0,
        "Cash-flow quality": 15.0,
        "Shareholder returns & ownership": 5.0,
    }
    assert sum(weights.values()) == 100.0


def test_eps_above_threshold_is_recorded_as_a_strength() -> None:
    analyzer = _analyzer_with_ratios(
        [{"name": "EPS", "company_value": 25.0, "sector_value": None}]
    )
    category = analyzer.analyze_profitability(analyzer.ratio_map())

    assert category.metrics["EPS"] == 25.0
    assert any("high-priority" in strength for strength in category.strengths)
    assert not category.risks


def test_eps_below_threshold_is_recorded_as_a_risk() -> None:
    analyzer = _analyzer_with_ratios(
        [{"name": "EPS", "company_value": 5.0, "sector_value": None}]
    )
    category = analyzer.analyze_profitability(analyzer.ratio_map())

    assert category.metrics["EPS"] == 5.0
    assert any("high-priority" in risk for risk in category.risks)


def test_missing_eps_adds_a_caveat_without_crashing() -> None:
    analyzer = _analyzer_with_ratios([])
    category = analyzer.analyze_profitability(analyzer.ratio_map())

    assert "EPS is unavailable." in category.caveats
    assert category.score is None


def test_profitability_score_reflects_all_weighted_inputs() -> None:
    analyzer = _analyzer_with_ratios(
        [
            {"name": "ROE", "company_value": 25.0, "sector_value": None},
            {"name": "ROCE", "company_value": 25.0, "sector_value": None},
            {"name": "ROA", "company_value": 12.0, "sector_value": None},
            {"name": "EPS", "company_value": 45.0, "sector_value": None},
        ]
    )
    category = analyzer.analyze_profitability(analyzer.ratio_map())

    assert category.score == pytest.approx(100.0)
