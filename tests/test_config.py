import pytest

from arbengine.config import Settings


def _settings(**overrides) -> Settings:
    base = {"kalshi_api_key_id": "test"}
    base.update(overrides)
    return Settings(**base)


def test_comma_separated_series_from_env(monkeypatch) -> None:
    """
    The form a human actually writes in .env. pydantic-settings treats list
    fields as complex and JSON-decodes them before validators run, so without
    NoDecode this raises JSONDecodeError instead of parsing.
    """
    monkeypatch.setenv("TARGET_SERIES", "KXBTCD,KXETHD,KXHIGHNY")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test")
    assert Settings(_env_file=None).target_series == ["KXBTCD", "KXETHD", "KXHIGHNY"]


def test_series_whitespace_is_trimmed(monkeypatch) -> None:
    monkeypatch.setenv("TARGET_SERIES", " KXBTCD , KXETHD ,, ")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test")
    assert Settings(_env_file=None).target_series == ["KXBTCD", "KXETHD"]


def test_json_array_series_still_works(monkeypatch) -> None:
    """The form the default decoder expected — kept working for compatibility."""
    monkeypatch.setenv("TARGET_SERIES", '["KXBTCD", "KXETHD"]')
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test")
    assert Settings(_env_file=None).target_series == ["KXBTCD", "KXETHD"]


def test_single_series_needs_no_comma(monkeypatch) -> None:
    monkeypatch.setenv("TARGET_SERIES", "KXBTCD")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test")
    assert Settings(_env_file=None).target_series == ["KXBTCD"]


def test_list_passed_directly_is_untouched() -> None:
    assert _settings(target_series=["A", "B"]).target_series == ["A", "B"]


def test_negative_fee_multiplier_rejected() -> None:
    with pytest.raises(ValueError):
        _settings(fee_multiplier=-0.01)


def test_fill_probability_must_be_a_probability() -> None:
    with pytest.raises(ValueError):
        _settings(paper_leg_fill_prob=1.5)
    with pytest.raises(ValueError):
        _settings(paper_leg_fill_prob=-0.1)
    assert _settings(paper_leg_fill_prob=0.75).paper_leg_fill_prob == 0.75
