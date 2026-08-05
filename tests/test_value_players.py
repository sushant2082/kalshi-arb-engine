"""
Tennis matching is safe on (player, date) because a player plays at most one
match per day — unlike a baseball series, where the same matchup repeats on
consecutive days and a date join silently spans different games.
"""

from datetime import datetime, timezone

from arbengine.value.players import (
    match_players,
    name_key,
    normalize_name,
    parse_contract,
)


def test_accents_are_normalized() -> None:
    assert name_key("Iva Jović") == name_key("Iva Jovic")
    assert normalize_name("Iva Jović") == "iva jovic"


def test_middle_names_do_not_break_matching() -> None:
    assert name_key("Leylah Fernandez") == name_key("Leylah Annie Fernandez")


def test_surname_particles_stay_with_the_surname() -> None:
    assert name_key("Botic van de Zandschulp") == "vandezandschulp|b"


def test_different_players_do_not_collide() -> None:
    """
    The key includes a first initial precisely so two players sharing a
    surname stay distinct. A collision here would price one player's contract
    against another's line.
    """
    assert name_key("Elena Rybakina") != name_key("Daria Kasatkina")
    assert name_key("A Williams") != name_key("S Williams")


def _sharp(away, home, start):
    return {
        "event_id": f"{away}-{home}", "away_team": away, "home_team": home,
        "commence_time": start, "home_implied": 0.52, "away_implied": 0.52,
    }


def _market(ticker, player):
    return {"ticker": ticker, "yes_sub_title": player}


def test_parses_a_tennis_contract() -> None:
    c = parse_contract(_market("KXWTAMATCH-26AUG05UDVOSA-OSA", "Naomi Osaka"))
    assert c.player == "Naomi Osaka"
    assert c.match_date.day == 5
    assert c.key == "osaka|n"


def test_matches_both_sides_of_one_match() -> None:
    start = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    markets = [
        _market("KXWTAMATCH-26AUG05UDVOSA-UDV", "Panna Udvardy"),
        _market("KXWTAMATCH-26AUG05UDVOSA-OSA", "Naomi Osaka"),
    ]
    matches, _ = match_players(
        markets, [_sharp("Panna Udvardy", "Naomi Osaka", start)]
    )
    assert len(matches) == 2
    assert {m.side_is_home for m in matches} == {True, False}


def test_unknown_player_is_rejected_not_guessed() -> None:
    start = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)
    matches, rejects = match_players(
        [_market("KXWTAMATCH-26AUG05ABCDEF-ABC", "Nobody Here")],
        [_sharp("Panna Udvardy", "Naomi Osaka", start)],
    )
    assert matches == []
    assert any("no sharp line" in r for r in rejects)


def test_wrong_date_does_not_match() -> None:
    start = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)
    matches, rejects = match_players(
        [_market("KXWTAMATCH-26AUG05UDVOSA-OSA", "Naomi Osaka")],
        [_sharp("Panna Udvardy", "Naomi Osaka", start)],
    )
    assert matches == []
    assert any("date" in r for r in rejects)


def test_a_player_in_two_sharp_matches_is_refused() -> None:
    """
    Should not happen in a real draw, but guessing between two candidate
    matches would price a contract against the wrong opponent.
    """
    d1 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    d2 = datetime(2026, 8, 5, 18, 0, tzinfo=timezone.utc)
    matches, rejects = match_players(
        [_market("KXWTAMATCH-26AUG05UDVOSA-OSA", "Naomi Osaka")],
        [_sharp("Panna Udvardy", "Naomi Osaka", d1),
         _sharp("Iva Jovic", "Naomi Osaka", d2)],
    )
    assert matches == []
    assert any("ambiguous" in r for r in rejects)
