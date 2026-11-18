"""Regression tests for fuzzy watchlist matching: unrelated titles that merely share
characters must not surface as "possible matches"."""
import pytest

import watchlist_checker as wc


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    # The values from the 2026-07-09 digest that produced the false positives.
    monkeypatch.setattr(wc, "MATCH_THRESHOLD", 0.82)
    monkeypatch.setattr(wc, "UNCERTAIN_FLOOR", 0.70)


def _match(showing, watchlisted):
    return wc.match_against_watchlist(
        [{"title": showing, "year": ""}], [(watchlisted, "", "https://letterboxd.com/film/x/")]
    )


FALSE_POSITIVES = [
    ("Toy Story 5", "Tokyo Story"),
    ("Crossing", "Crashing"),
    ("Amelie", "Alien"),
    ("Honeyjoon", "Honey Don't!"),
    ("Little Darlings", "The Little Prince"),
]


@pytest.mark.parametrize("showing,watchlisted", FALSE_POSITIVES)
def test_known_false_positives_no_longer_surface(showing, watchlisted):
    # They did score above the uncertain floor, which is why they used to appear.
    assert wc.similarity(showing, watchlisted) >= 0.70
    assert _match(showing, watchlisted) == ([], [])


@pytest.mark.parametrize("showing,watchlisted", [
    ("Blade Runner 2049", "Blade Runner"),
    ("Dune Part Two", "Dune"),
    ("Kill Bill Vol 1", "Kill Bill: Volume 1"),
    ("Star Wars Episode IV A New Hope", "Star Wars"),
    ("Amelie", "Amélie"),
])
def test_titles_sharing_most_words_still_surface_as_uncertain(showing, watchlisted, monkeypatch):
    monkeypatch.setattr(wc, "MATCH_THRESHOLD", 0.99)   # force the "uncertain" tier
    monkeypatch.setattr(wc, "UNCERTAIN_FLOOR", 0.30)  # isolate the word rule from the score floor
    matches, uncertain = _match(showing, watchlisted)
    assert matches == [] and [u["wl_title"] for u in uncertain] == [watchlisted]


def test_confident_matches_are_not_subject_to_the_word_rule():
    # One-letter typo in a one-word title: no shared word, but the score is high.
    matches, uncertain = _match("Frankenstien", "Frankenstein")
    assert [m["wl_title"] for m in matches] == ["Frankenstein"] and uncertain == []


def test_gate_falls_through_to_the_next_best_candidate():
    watchlist = [("Tokyo Story", "", "bad"), ("Toy Story", "", "good")]
    matches, uncertain = wc.match_against_watchlist([{"title": "Toy Story 5", "year": ""}], watchlist)
    assert [u["lb_url"] for u in matches + uncertain] == ["good"]


@pytest.mark.parametrize("a,b,expected", [
    ("Toy Story 5", "Tokyo Story", False),            # 1 of 2 shared words is not "more than half"
    ("Blade Runner 2049", "Blade Runner", True),
    ("The Little Prince", "Little Prince", True),     # articles are ignored
    ("Amelie", "Amélie", True),                       # accents are ignored
    ("", "Anything", False),
])
def test_shares_enough_words(a, b, expected):
    assert wc.shares_enough_words(a, b) is expected
