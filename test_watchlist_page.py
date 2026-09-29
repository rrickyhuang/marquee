"""Tests for parsing the scraped Letterboxd watchlist page."""
import watchlist_checker

SAMPLE = """
<li class="griditem">
<div class="react-component" data-component-class="LazyPoster" data-item-name="Behemoth! (2026)" data-item-slug="behemoth-2026" data-item-link="/film/behemoth-2026/" data-item-full-display-name="Behemoth! (2026)" data-postered-identifier='{&quot;lid&quot;:&quot;x&quot;}'></div>
</li>
<li class="griditem">
<div class="react-component" data-item-link="/film/fast-furious/" data-item-full-display-name="Fast &amp; Furious (2001)"></div>
</li>
<div class="react-component" data-component-class="NavComponent"></div>
"""


def test_parse_watchlist_page_extracts_title_year_url():
    assert watchlist_checker._parse_watchlist_page(SAMPLE) == [
        ("Behemoth!", "2026", "https://letterboxd.com/film/behemoth-2026/"),
        ("Fast & Furious", "2001", "https://letterboxd.com/film/fast-furious/"),
    ]


def test_parse_watchlist_page_empty():
    assert watchlist_checker._parse_watchlist_page("<html></html>") == []


def test_years_compatible():
    assert watchlist_checker.years_compatible("2026", "2025")   # within tolerance
    assert not watchlist_checker.years_compatible("2026", "2002")
    assert watchlist_checker.years_compatible("", "2002")       # unknown never conflicts
    assert watchlist_checker.years_compatible("2026", None)


def test_match_skips_year_conflicting_remake_and_unrelated_fuzzy():
    film = lambda t, y: {"title": t, "year": y, "showtimes": [], "url": ""}
    watchlist = [("Resident Evil", "2002", "u1"), ("Cruising", "1980", "u2"), ("Akira", "1988", "u3")]
    matches, uncertain = watchlist_checker.match_against_watchlist(
        [film("Resident Evil", "2026"), film("The Uprising", "2026"), film("Akira", "1988")], watchlist
    )
    assert [m["title"] for m in matches] == ["Akira"]
    assert uncertain == []
