"""Letterboxd links for films that aren't on the watchlist: slug when it's a safe
bet, search URL when the slug would be a guess."""
import pytest

import recommendations
import watchlist_checker

MODULES = [watchlist_checker, recommendations]


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("title,slug", [
    ("Dune", "dune"),
    ("Dune: Part Two", "dune-part-two"),
    ("Fast & Furious", "fast-furious"),
    ("Spider-Man: Beyond the Spider-Verse", "spider-man-beyond-the-spider-verse"),
    ("Schindler's List", "schindlers-list"),
    ("Schindler’s List", "schindlers-list"),
    ("Mission: Impossible 2", "mission-impossible-2"),
    ("Whiplash!", "whiplash"),
])
def test_ordinary_titles_keep_the_slug_url(module, title, slug):
    assert module.lb_film_url(title, "2020") == f"https://letterboxd.com/film/{slug}/"


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("title", [
    "Amélie",                  # non-ASCII: Letterboxd slugs are ASCII
    "Alien³",
    "Nosferatu (1922)",           # parentheses
    "Toy Story 3D",               # format suffix
    "The Matrix IMAX",
    "Jaws Q&A",
    "Back to the Future 40th Anniversary",
    "Casablanca - Dolby Atmos",
])
def test_risky_titles_fall_back_to_search(module, title):
    url = module.lb_film_url(title, "1999")
    assert url.startswith("https://letterboxd.com/search/")
    assert "1999" in url


@pytest.mark.parametrize("module", MODULES)
def test_empty_or_symbol_only_title_falls_back_to_search(module):
    assert module.lb_film_url("", "").startswith("https://letterboxd.com/search/")
    assert module.lb_film_url("!!!", "2001").startswith("https://letterboxd.com/search/")
