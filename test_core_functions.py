"""Unit tests for the core pure functions in watchlist_checker.py."""
from datetime import date

import pytest

import watchlist_checker as wc


# ── parse_relative_date ───────────────────────────────────────────────────────

TODAY = date(2026, 3, 18)


def test_parse_today_and_tomorrow_any_case():
    assert wc.parse_relative_date("Today", TODAY) == TODAY
    assert wc.parse_relative_date("  tomorrow ", TODAY) == date(2026, 3, 19)


@pytest.mark.parametrize("text", ["Mar 21", "March 21", "Fri Mar 21", "Friday March 21", "Fri March 21", "Friday Mar 21"])
def test_parse_month_day_formats(text):
    assert wc.parse_relative_date(text, TODAY) == date(2026, 3, 21)


def test_parse_year_rollover_in_december():
    assert wc.parse_relative_date("Jan 5", date(2026, 12, 20)) == date(2027, 1, 5)


def test_parse_recent_past_stays_in_current_year():
    assert wc.parse_relative_date("Mar 1", TODAY) == date(2026, 3, 1)


def test_parse_rollover_boundary_is_sixty_days():
    # exactly 60 days back stays in this year; 61 rolls to next year
    assert wc.parse_relative_date("Jan 17", TODAY) == date(2026, 1, 17)
    assert wc.parse_relative_date("Jan 16", TODAY) == date(2027, 1, 16)


def test_parse_same_day_year_end():
    assert wc.parse_relative_date("Dec 31", date(2026, 12, 31)) == date(2026, 12, 31)


@pytest.mark.parametrize("text", ["", "soon", "Smarch 40", "2026-03-21"])
def test_parse_unparseable_returns_none(text):
    assert wc.parse_relative_date(text, TODAY) is None


# ── fmt_time ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("0000", "12:00am"),
    ("0005", "12:05am"),
    ("930", "9:30am"),
    ("0930", "9:30am"),
    ("1159", "11:59am"),
    ("1200", "12:00pm"),
    ("1230", "12:30pm"),
    ("1300", "1:00pm"),
    ("2130", "9:30pm"),
    ("2359", "11:59pm"),
])
def test_fmt_time(raw, expected):
    assert wc.fmt_time(raw) == expected


# ── normalize / slugify ───────────────────────────────────────────────────────

@pytest.mark.parametrize("title,expected", [
    ("The Godfather", "godfather"),
    ("A Quiet Place: Day One", "quiet place day one"),
    ("An Autumn Tale", "autumn tale"),
    ("Fast & Furious", "fast furious"),
    ("  Spaced   Out!  ", "spaced out"),
    ("Theodora", "theodora"),  # article stripping only applies to whole words
])
def test_normalize(title, expected):
    assert wc.normalize(title) == expected


@pytest.mark.parametrize("title,expected", [
    ("Fast & Furious", "fast-furious"),
    ("  Spaced   Out  ", "spaced-out"),
    ("Mad Max: Fury Road", "mad-max-fury-road"),
    ("Blade_Runner 2049", "blade-runner-2049"),
    ("!!!", ""),
])
def test_slugify(title, expected):
    assert wc.slugify(title) == expected


# ── extract_dated_showtimes ───────────────────────────────────────────────────

def test_extract_dated_showtimes_assigns_times_to_preceding_header():
    block = (
        '<u>Thu <span class="timesdate">Mar 19</span></u>'
        '<i><span class="tix" data-time="1130">11:30am </span>'
        '<span class="tix" data-time="1945">7:45pm </span></i>'
        '<u>Fri <span class="timesdate">Mar 20</span></u>'
        '<i><span class="notix" data-time="2130">9:30pm </span></i>'
    )
    assert wc.extract_dated_showtimes(block, TODAY) == [
        {"date": "2026-03-19", "time": "11:30am"},
        {"date": "2026-03-19", "time": "7:45pm"},
        {"date": "2026-03-20", "time": "9:30pm"},
    ]


def test_extract_dated_showtimes_handles_today_header_and_empty_block():
    block = '<span class="timesdate">Today</span><span class="tix" data-time="1800">6:00pm</span>'
    assert wc.extract_dated_showtimes(block, TODAY) == [{"date": "2026-03-18", "time": "6:00pm"}]
    assert wc.extract_dated_showtimes("<div>no showtimes</div>", TODAY) == []


# ── match_against_watchlist ───────────────────────────────────────────────────

def _film(title, year=""):
    return {"title": title, "year": year}


@pytest.fixture
def thresholds(monkeypatch):
    def set_(match, floor):
        monkeypatch.setattr(wc, "MATCH_THRESHOLD", match)
        monkeypatch.setattr(wc, "UNCERTAIN_FLOOR", floor)
    set_(0.82, 0.70)
    return set_


def test_exact_title_is_confident_and_carries_watchlist_details(thresholds):
    wl = [("The Brutalist", "2024", "https://letterboxd.com/film/the-brutalist/")]
    matches, uncertain = wc.match_against_watchlist([_film("Brutalist", "2024")], wl)
    assert uncertain == []
    assert len(matches) == 1
    assert matches[0]["wl_title"] == "The Brutalist"
    assert matches[0]["wl_year"] == "2024"
    assert matches[0]["lb_url"] == "https://letterboxd.com/film/the-brutalist/"
    assert matches[0]["score"] == 1.0


def test_unrelated_title_does_not_match(thresholds):
    wl = [("Stalker", "1979", "u")]
    assert wc.match_against_watchlist([_film("Paddington", "2014")], wl) == ([], [])


def test_picks_the_best_watchlist_entry(thresholds):
    wl = [("Heat 2", "2030", "u1"), ("Heat", "1995", "u2")]
    matches, _ = wc.match_against_watchlist([_film("Heat", "1995")], wl)
    assert [m["lb_url"] for m in matches] == ["u2"]


def test_threshold_banding(thresholds):
    a, b = "Blade Runner 2049", "Blade Runner"
    score = wc.similarity(a, b)
    wl = [(b, "", "u")]
    film = [_film(a)]

    thresholds(score, score - 0.1)            # score == threshold counts as confident
    assert [len(x) for x in wc.match_against_watchlist(film, wl)] == [1, 0]

    thresholds(score + 0.01, score - 0.01)    # between floor and threshold: uncertain
    assert [len(x) for x in wc.match_against_watchlist(film, wl)] == [0, 1]

    thresholds(score + 0.02, score)           # score == floor counts as uncertain
    assert [len(x) for x in wc.match_against_watchlist(film, wl)] == [0, 1]

    thresholds(score + 0.02, score + 0.01)    # below the floor: dropped
    assert wc.match_against_watchlist(film, wl) == ([], [])


def test_year_conflict_with_exact_title_is_only_uncertain(thresholds):
    wl = [("Dune", "2021", "u")]
    matches, uncertain = wc.match_against_watchlist([_film("Dune", "1984")], wl)
    assert matches == []
    assert [u["wl_title"] for u in uncertain] == ["Dune"]


def test_year_conflict_with_inexact_title_is_dropped(thresholds):
    thresholds(0.5, 0.4)  # permissive, so only the year rule can reject it
    wl = [("Dune Part Two", "2024", "u")]
    assert wc.match_against_watchlist([_film("Dune", "1984")], wl) == ([], [])


def test_year_within_tolerance_or_unknown_is_confident(thresholds):
    wl = [("Anora", "2024", "u")]
    for year in ("2024", "2025", ""):
        matches, _ = wc.match_against_watchlist([_film("Anora", year)], wl)
        assert len(matches) == 1, year


def test_matching_year_beats_conflicting_year_for_same_title(thresholds):
    wl = [("Dune", "1984", "old"), ("Dune", "2021", "new")]
    matches, _ = wc.match_against_watchlist([_film("Dune", "2021")], wl)
    assert [m["lb_url"] for m in matches] == ["new"]
