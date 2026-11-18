"""Tests for run-health reporting: showtime dating, scrape status, watchlist source,
and how failures surface in the digest email and exit status."""
from datetime import date

import watchlist_checker as wc

TODAY = date(2026, 3, 18)


# ── extract_dated_showtimes: no guessing at dates ─────────────────────────────

def test_showtimes_before_any_header_are_dropped_and_counted():
    stats = {}
    block = '<span class="tix" data-time="1800">6:00pm</span>'
    assert wc.extract_dated_showtimes(block, TODAY, stats) == []
    assert stats == {"undated_showtimes": 1}


def test_unparseable_header_drops_its_showtimes_instead_of_reusing_previous_day():
    stats = {}
    block = (
        '<span class="timesdate">Mar 19</span><span class="tix" data-time="1130">x</span>'
        '<span class="timesdate">Smarch 40</span><span class="tix" data-time="1400">x</span>'
        '<span class="timesdate">Mar 21</span><span class="tix" data-time="2000">x</span>'
    )
    assert wc.extract_dated_showtimes(block, TODAY, stats) == [
        {"date": "2026-03-19", "time": "11:30am"},
        {"date": "2026-03-21", "time": "8:00pm"},
    ]
    assert stats == {"unparsed_headers": 1, "undated_showtimes": 1}


def test_clean_block_reports_no_drops():
    stats = {}
    block = '<span class="timesdate">Today</span><span class="tix" data-time="1800">x</span>'
    assert len(wc.extract_dated_showtimes(block, TODAY, stats)) == 1
    assert stats == {}


def test_stats_argument_is_optional():
    assert wc.extract_dated_showtimes('<span class="tix" data-time="1800">x</span>', TODAY) == []


# ── scrape_cinemaclock: status instead of silent [] ───────────────────────────

import pytest


def _page(*blocks):
    return "<html>" + "".join(blocks) + "</html>"


def _block(title, slug, header="Today", times=("1930",)):
    tix = "".join(f'<span class="tix" data-time="{t}">x</span>' for t in times)
    return (
        f'<div class="showtimeblock movie"><h3 class=\'movietitle\'><a href=\'/movies/{slug}\'>{title}</a></h3>'
        f'<p class=\'moviegenre\'>Drama 1h30m</p>'
        f'<u><span class="timesdate">{header}</span></u><i>{tix}</i></div>'
    )


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(wc.time, "sleep", slept.append)
    return slept


def test_healthy_scrape_is_ok(monkeypatch, no_sleep):
    monkeypatch.setattr(wc, "fetch_html", lambda url: _page(_block("Dune", "dune-2021")))
    films, status = wc.scrape_cinemaclock("http://x")
    assert status == {"state": "ok", "reason": ""}
    assert [f["title"] for f in films] == ["Dune"]
    assert films[0]["showtimes"][0]["date"] == date.today().isoformat()


def test_network_error_is_retried_once_then_reported_as_failed(monkeypatch, no_sleep):
    calls = []
    def boom(url):
        calls.append(url)
        raise OSError("timed out")
    monkeypatch.setattr(wc, "fetch_html", boom)
    films, status = wc.scrape_cinemaclock("http://x")
    assert films == []
    assert status["state"] == "failed" and "timed out" in status["reason"]
    assert len(calls) == 2 and len(no_sleep) == 1


def test_transient_error_recovers_on_retry(monkeypatch, no_sleep):
    pages = iter([OSError("blip"), _page(_block("Dune", "dune-2021"))])
    def flaky(url):
        p = next(pages)
        if isinstance(p, Exception):
            raise p
        return p
    monkeypatch.setattr(wc, "fetch_html", flaky)
    films, status = wc.scrape_cinemaclock("http://x")
    assert status["state"] == "ok" and len(films) == 1


def test_page_with_no_listings_is_suspicious(monkeypatch, no_sleep):
    monkeypatch.setattr(wc, "fetch_html", lambda url: "<html><body>Welcome!</body></html>")
    films, status = wc.scrape_cinemaclock("http://x")
    assert films == []
    assert status["state"] == "suspicious" and "no film listings" in status["reason"]


def test_listings_without_readable_showtimes_are_suspicious(monkeypatch, no_sleep):
    monkeypatch.setattr(wc, "fetch_html", lambda url: _page(_block("Dune", "dune-2021", times=())))
    films, status = wc.scrape_cinemaclock("http://x")
    assert len(films) == 1
    assert status["state"] == "suspicious" and "no showtimes" in status["reason"]


def test_dropped_showtimes_make_the_scrape_suspicious(monkeypatch, no_sleep):
    page = _page(_block("Dune", "dune-2021"), _block("Heat", "heat-1995", header="Smarch 40"))
    monkeypatch.setattr(wc, "fetch_html", lambda url: page)
    films, status = wc.scrape_cinemaclock("http://x")
    assert len(films) == 2
    assert status["state"] == "suspicious" and "1 showtime(s) skipped" in status["reason"]
