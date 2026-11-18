"""Tests for run-health reporting: showtime dating, scrape status, watchlist source,
and how failures surface in the digest email and exit status."""
import os
import time
from datetime import date

import pytest

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


# ── load_watchlist: which source, and why ─────────────────────────────────────


def test_live_watchlist_is_reported_as_live(monkeypatch):
    monkeypatch.setattr(wc, "fetch_watchlist_page", lambda errors=None: [("Dune", "2021", "u")])
    entries, info = wc.load_watchlist()
    assert entries == [("Dune", "2021", "u")]
    assert info == {"source": "live", "count": 1, "zip_age_days": None, "reason": ""}


def _failing_live(reason):
    def fetch(errors=None):
        errors.append(reason)
        return None
    return fetch


def test_zip_fallback_reports_source_age_and_live_failure(monkeypatch, tmp_path):
    zip_path = tmp_path / "letterboxd-x.zip"
    zip_path.write_bytes(b"")
    os.utime(zip_path, (time.time() - 20 * 86400,) * 2)
    monkeypatch.setattr(wc, "fetch_watchlist_page", _failing_live("HTTP 403 on page 1"))
    monkeypatch.setattr(wc, "load_letterboxd_export", lambda: (set(), [("Heat", "1995", "u")]))
    monkeypatch.setattr(wc.glob, "glob", lambda pattern: [str(zip_path)])
    entries, info = wc.load_watchlist()
    assert entries == [("Heat", "1995", "u")]
    assert info["source"] == "zip" and info["zip_age_days"] == 20
    assert "HTTP 403 on page 1" in info["reason"]


def test_no_live_and_no_zip_is_an_empty_watchlist_with_reason(monkeypatch):
    monkeypatch.setattr(wc, "fetch_watchlist_page", _failing_live("curl_cffi is not installed"))
    monkeypatch.setattr(wc, "load_letterboxd_export", lambda: (set(), []))
    entries, info = wc.load_watchlist()
    assert entries == []
    assert info["source"] == "none" and info["count"] == 0
    assert "curl_cffi is not installed" in info["reason"] and "no export ZIP" in info["reason"]


# ── main(): an empty watchlist is a failed run, not a quiet one ───────────────

def test_empty_watchlist_sends_failure_email_and_exits_nonzero(monkeypatch):
    sent = []
    monkeypatch.setattr(wc, "load_watched_titles", lambda: (set(), ""))
    monkeypatch.setattr(wc, "load_watchlist", lambda: ([], {"source": "none", "count": 0, "zip_age_days": None, "reason": "it broke"}))
    monkeypatch.setattr(wc, "scrape_cinemaclock", lambda url: pytest.fail("must not scrape without a watchlist"))
    monkeypatch.setattr(wc, "THEATRES", {"A": "http://a"})
    monkeypatch.setattr(wc, "send_email", lambda subject, html: sent.append((subject, html)) or True)
    assert wc.main() == 1
    assert len(sent) == 1
    subject, html = sent[0]
    assert "run failed" in subject and subject.startswith("⚠")
    assert "it broke" in html
    assert "Theatres checked" not in html


# ── main(): degraded runs are visible in the email and the exit status ────────

LIVE = {"source": "live", "count": 1, "zip_age_days": None, "reason": ""}
OK = {"state": "ok", "reason": ""}


def _run_main(monkeypatch, statuses, watchlist_info=LIVE, watched_note="", send_result=True):
    """Run main() against two fake theatres; returns (exit_status, [(subject, html), ...])."""
    sent = []
    film = {"title": "Dune", "year": "2021", "runtime": "", "url": "x",
            "showtimes": [{"date": date.today().isoformat(), "time": "7:30pm"}]}
    other = {**film, "title": "Heat", "year": "1995"}  # not on the watchlist → "everything playing" email
    scrapes = {
        url: ([film, other], st) if st["state"] == "ok" else ([], st)
        for url, st in (("http://a", statuses["A"]), ("http://b", statuses["B"]))
    }
    monkeypatch.setattr(wc, "load_watched_titles", lambda: (set(), watched_note))
    monkeypatch.setattr(wc, "load_watchlist", lambda: ([("Dune", "2021", "https://letterboxd.com/film/dune-2021/")], watchlist_info))
    monkeypatch.setattr(wc, "scrape_cinemaclock", lambda url: scrapes[url])
    monkeypatch.setattr(wc, "THEATRES", {"Alpha Cinema": "http://a", "Beta Cinema": "http://b"})
    monkeypatch.setattr(wc, "THEATRE_HOMEPAGES", {"Alpha Cinema": "http://home-a", "Beta Cinema": "http://home-b"})
    monkeypatch.setattr(wc, "send_email", lambda subject, html: sent.append((subject, html)) or send_result)
    status = wc.main()
    return status, sent


def _statuses(a=OK, b=OK):
    return {"A": a, "B": b}


def test_clean_run_has_no_warning_and_exits_zero(monkeypatch):
    status, sent = _run_main(monkeypatch, _statuses())
    assert status == 0
    subject, html = sent[0]
    assert not subject.startswith("⚠")
    assert "This run had problems" not in html and "Heads up" not in html
    assert "Alpha Cinema" in html and "Beta Cinema" in html
    assert "Not checked" not in html


def test_failed_theatre_is_flagged_everywhere_and_exits_nonzero(monkeypatch):
    failed = {"state": "failed", "reason": "could not fetch the page (HTTP Error 404)"}
    status, sent = _run_main(monkeypatch, _statuses(b=failed))
    assert status == 1
    for subject, html in sent:                      # digest and all-films email
        assert subject.startswith("⚠")
        assert "This run had problems" in html
        assert "Beta Cinema: could not fetch the page (HTTP Error 404)" in html
        assert "Not checked (see warning above):</b> Beta Cinema" in html
        assert 'href="http://b"' not in html        # not listed as a checked theatre
        assert 'href="http://a"' in html
    assert len(sent) == 2


def test_suspicious_theatre_also_counts_as_a_problem(monkeypatch):
    odd = {"state": "suspicious", "reason": "no film listings found on the page"}
    status, sent = _run_main(monkeypatch, _statuses(a=odd))
    assert status == 1 and "no film listings found" in sent[0][1]


def test_recent_zip_fallback_warns_but_exits_zero(monkeypatch):
    info = {"source": "zip", "count": 1, "zip_age_days": 3, "reason": "the live watchlist scrape failed (HTTP 403)"}
    status, sent = _run_main(monkeypatch, _statuses(), watchlist_info=info)
    assert status == 0
    subject, html = sent[0]
    assert subject.startswith("⚠") and "Heads up" in html
    assert "export ZIP (3 day(s) old)" in html and "HTTP 403" in html


def test_stale_zip_fallback_exits_nonzero(monkeypatch):
    info = {"source": "zip", "count": 1, "zip_age_days": 30, "reason": "the live watchlist scrape failed (HTTP 403)"}
    status, sent = _run_main(monkeypatch, _statuses(), watchlist_info=info)
    assert status == 1 and "30 day(s) old" in sent[0][1]


def test_watched_titles_note_is_a_notice(monkeypatch):
    status, sent = _run_main(monkeypatch, _statuses(), watched_note="Recent watches could not be loaded")
    assert status == 0
    assert sent[0][0].startswith("⚠") and "Recent watches could not be loaded" in sent[0][1]


def test_email_send_failure_exits_nonzero(monkeypatch):
    status, _ = _run_main(monkeypatch, _statuses(), send_result=False)
    assert status == 1


def test_health_arguments_are_optional():
    html = wc.build_email_html({}, {"Alpha Cinema": "http://home-a"})
    assert "This run had problems" not in html and "Heads up" not in html
    assert "Alpha Cinema" in html
