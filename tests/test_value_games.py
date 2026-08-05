"""
Match on first pitch, not on date. Baseball plays the same matchup on
consecutive days, so a date-based join maps one sharp line onto every game of
the series — including games already decided.
"""

from datetime import datetime, timedelta, timezone

from arbengine.value.games import match_games, parse_ticker, split_team_pair


def test_ticker_start_time_converts_from_eastern_to_utc() -> None:
    g = parse_ticker("KXMLBGAME-26AUG042140SDAZ-SD")
    assert g.start_utc == datetime(2026, 8, 5, 1, 40, tzinfo=timezone.utc)


def test_ambiguous_team_pairs_are_rejected_not_guessed() -> None:
    assert split_team_pair("SDAZ") == ("SD", "AZ")
    assert split_team_pair("DETSEA") == ("DET", "SEA")
    assert split_team_pair("ZZZZ") is None


def _sharp(away, home, start):
    return {
        "event_id": f"{away}@{home}@{start:%m%d%H%M}",
        "away_team": away, "home_team": home,
        "commence_time": start,
        "home_implied": 0.52, "away_implied": 0.52,
    }


def test_a_series_does_not_collapse_onto_one_sharp_line() -> None:
    """
    The bug this guards. SD @ AZ on Aug 4, 5 and 6 are three different games;
    a single Aug 5 sharp line must match only the Aug 5 contracts. Measured
    live, the date-based join matched all six contracts, and the already-
    decided Aug 4 game (0.97/0.04) then read as a double-digit edge.
    """
    markets = [
        {"ticker": f"KXMLBGAME-26AUG0{d}2140SDAZ-{side}"}
        for d in (4, 5, 6) for side in ("SD", "AZ")
    ]
    aug5 = datetime(2026, 8, 5, 1, 40, tzinfo=timezone.utc)
    matches, _ = match_games(markets, [_sharp("San Diego Padres",
                                              "Arizona Diamondbacks", aug5)])
    assert len(matches) == 2, "only the Aug 4 ET (Aug 5 UTC) game should match"
    assert all(m.game.game_date.day == 4 for m in matches)


def test_wrong_start_time_is_rejected() -> None:
    markets = [{"ticker": "KXMLBGAME-26AUG042140SDAZ-SD"}]
    far = datetime(2026, 8, 5, 6, 0, tzinfo=timezone.utc)  # 4h+ later
    matches, rejects = match_games(markets, [_sharp("San Diego Padres",
                                                    "Arizona Diamondbacks", far)])
    assert matches == []
    assert any("start time" in r for r in rejects)


def test_each_sharp_game_matches_exactly_two_contracts() -> None:
    start = datetime(2026, 8, 5, 1, 40, tzinfo=timezone.utc)
    markets = [
        {"ticker": "KXMLBGAME-26AUG042140SDAZ-SD"},
        {"ticker": "KXMLBGAME-26AUG042140SDAZ-AZ"},
    ]
    matches, _ = match_games(markets, [_sharp("San Diego Padres",
                                              "Arizona Diamondbacks", start)])
    assert len(matches) == 2
    assert {m.side_is_home for m in matches} == {True, False}
