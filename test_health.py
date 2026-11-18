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
