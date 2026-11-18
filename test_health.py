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


# ═════════════════════════════════════════════════════════════════════════════
# recommendations.py
# ═════════════════════════════════════════════════════════════════════════════

import sys
import types

import recommendations as rc


# ── scraping ──────────────────────────────────────────────────────────────────

def test_recs_showtimes_without_a_usable_header_are_dropped():
    stats = {}
    block = (
        '<span class="tix" data-time="1000">x</span>'
        '<span class="timesdate">Smarch 40</span><span class="tix" data-time="1400">x</span>'
        '<span class="timesdate">Mar 21</span><span class="tix" data-time="2000">x</span>'
    )
    assert rc.extract_dated_showtimes(block, TODAY, stats) == [{"date": "2026-03-21", "time": "8:00pm"}]
    assert stats == {"undated_showtimes": 2, "unparsed_headers": 1}


def test_recs_scrape_statuses(monkeypatch, no_sleep):
    monkeypatch.setattr(rc, "fetch_html", lambda url: _page(_block("Dune", "dune-2021")))
    films, status = rc.scrape_cinemaclock("http://x")
    assert status["state"] == "ok" and [f["title"] for f in films] == ["Dune"]

    monkeypatch.setattr(rc, "fetch_html", lambda url: "<html>nothing here</html>")
    films, status = rc.scrape_cinemaclock("http://x")
    assert films == [] and status["state"] == "suspicious"

    monkeypatch.setattr(rc, "fetch_html", lambda url: _page(_block("Dune", "dune-2021", times=())))
    assert rc.scrape_cinemaclock("http://x")[1]["state"] == "suspicious"

    def boom(url):
        raise OSError("timed out")
    monkeypatch.setattr(rc, "fetch_html", boom)
    films, status = rc.scrape_cinemaclock("http://x")
    assert films == [] and status["state"] == "failed" and "timed out" in status["reason"]


# ── the Claude step reports why it failed ─────────────────────────────────────

def _fake_anthropic(monkeypatch, text="[]", stop_reason="end_turn", error=None):
    class Messages:
        def create(self, **kwargs):
            if error:
                raise error
            return types.SimpleNamespace(content=[types.SimpleNamespace(text=text)], stop_reason=stop_reason)
    class Client:
        def __init__(self, api_key):
            self.messages = Messages()
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    monkeypatch.setattr(rc, "ANTHROPIC_API_KEY", "test-key")


FILMS = [{"title": "Heat", "year": "1995"}]


def test_missing_api_key_raises_with_reason(monkeypatch):
    monkeypatch.setattr(rc, "ANTHROPIC_API_KEY", "")
    with pytest.raises(rc.RecommendationError, match="ANTHROPIC_API_KEY"):
        rc.get_recommendations(FILMS)


def test_api_error_raises_with_reason(monkeypatch):
    _fake_anthropic(monkeypatch, error=RuntimeError("model not found"))
    with pytest.raises(rc.RecommendationError, match="Claude API error: model not found"):
        rc.get_recommendations(FILMS)


def test_truncated_reply_raises_with_reason(monkeypatch):
    _fake_anthropic(monkeypatch, text='[{"title": "He', stop_reason="max_tokens")
    with pytest.raises(rc.RecommendationError, match="max_tokens"):
        rc.get_recommendations(FILMS)


def test_invalid_json_raises_with_reason(monkeypatch):
    _fake_anthropic(monkeypatch, text="Sure! Here are some films")
    with pytest.raises(rc.RecommendationError, match="valid JSON"):
        rc.get_recommendations(FILMS)


def test_valid_reply_is_parsed_even_in_code_fences(monkeypatch):
    _fake_anthropic(monkeypatch, text='```json\n[{"title": "Heat", "year": "1995", "reason": "r"}]\n```')
    assert rc.get_recommendations(FILMS) == [{"title": "Heat", "year": "1995", "reason": "r"}]


# ── main(): every failure path emails a reason and exits non-zero ─────────────

import watchlist_checker


def _showing(title, year):
    return {"title": title, "year": year,
            "showtimes": [{"date": date.today().isoformat(), "time": "7:30pm"}]}


def _run_recs(monkeypatch, *, live=("Dune", "2021", "u"), zip_entries=(), live_error="HTTP 403",
              statuses=None, recs=None, claude_error=None, send_result=True, films=None):
    """Run recommendations.main() with everything external faked.
    Returns (exit_status, [(subject, html), ...])."""
    sent = []
    statuses = statuses or {"Alpha Cinema": OK, "Beta Cinema": OK}
    films = films if films is not None else [_showing("Dune", "2021"), _showing("Heat", "1995")]
    recs = recs if recs is not None else [{"title": "Heat", "year": "1995", "reason": "Because."}]

    def fetch_live(errors=None):
        if live is None:
            errors.append(live_error)
            return None
        return [live]
    monkeypatch.setattr(watchlist_checker, "fetch_watchlist_page", fetch_live)
    monkeypatch.setattr(rc, "fetch_watched_titles", lambda: set())
    monkeypatch.setattr(rc, "load_letterboxd_export", lambda: (set(), list(zip_entries)))
    monkeypatch.setattr(rc, "THEATRES", {"Alpha Cinema": "http://a", "Beta Cinema": "http://b"})
    monkeypatch.setattr(rc, "THEATRE_HOMEPAGES", {"Alpha Cinema": "http://home-a", "Beta Cinema": "http://home-b"})
    monkeypatch.setattr(rc, "scrape_cinemaclock",
                        lambda url: (films, statuses["Alpha Cinema"]) if url == "http://a"
                        else (films if statuses["Beta Cinema"]["state"] == "ok" else [], statuses["Beta Cinema"]))

    def get_recs(eligible):
        if claude_error:
            raise rc.RecommendationError(claude_error)
        return recs
    monkeypatch.setattr(rc, "get_recommendations", get_recs)
    monkeypatch.setattr(rc, "send_email", lambda subject, html: sent.append((subject, html)) or send_result)
    return rc.main(), sent


def test_recs_clean_run(monkeypatch):
    status, sent = _run_recs(monkeypatch)
    assert status == 0 and len(sent) == 1
    subject, html = sent[0]
    assert not subject.startswith("⚠") and "This run" not in html and "Heat" in html
    assert "Not checked" not in html


def test_recs_unavailable_watchlist_fails_the_run(monkeypatch):
    status, sent = _run_recs(monkeypatch, live=None, live_error="HTTP 403 on page 1")
    assert status == 1 and len(sent) == 1
    subject, html = sent[0]
    assert subject.startswith("⚠") and "run failed" in subject
    assert "watchlist could not be loaded" in html and "HTTP 403 on page 1" in html
    assert "Theatres checked" not in html


def test_recs_zip_watchlist_fallback_warns_but_still_sends(monkeypatch):
    status, sent = _run_recs(monkeypatch, live=None, zip_entries=[("Dune", "2021", "u")])
    assert status == 0
    subject, html = sent[0]
    assert subject.startswith("⚠") and "export ZIP" in html and "HTTP 403" in html and "Heat" in html


def test_recs_all_theatres_unreadable_fails_the_run(monkeypatch):
    bad = {"state": "failed", "reason": "could not fetch the page (HTTP Error 404)"}
    status, sent = _run_recs(monkeypatch, statuses={"Alpha Cinema": bad, "Beta Cinema": bad}, films=[])
    assert status == 1 and "run failed" in sent[0][0]
    assert "No films could be read from any theatre" in sent[0][1]
    assert "Alpha Cinema: could not fetch the page" in sent[0][1]


def test_recs_one_unreadable_theatre_is_flagged_and_exits_nonzero(monkeypatch):
    bad = {"state": "failed", "reason": "could not fetch the page (HTTP Error 404)"}
    status, sent = _run_recs(monkeypatch, statuses={"Alpha Cinema": OK, "Beta Cinema": bad})
    assert status == 1 and len(sent) == 1
    subject, html = sent[0]
    assert subject.startswith("⚠") and "Heat" in html
    assert "Beta Cinema: could not fetch the page" in html
    assert "Not checked (see warning above):</b> Beta Cinema" in html


def test_recs_claude_failure_is_emailed_with_reason(monkeypatch):
    status, sent = _run_recs(monkeypatch, claude_error="Claude API error: model not found")
    assert status == 1 and "run failed" in sent[0][0]
    assert "model not found" in sent[0][1]


def test_recs_empty_claude_reply_fails_the_run(monkeypatch):
    status, sent = _run_recs(monkeypatch, recs=[])
    assert status == 1 and "Claude returned no recommendations" in sent[0][1]


def test_recs_picks_that_match_no_showing_send_the_soft_note(monkeypatch):
    status, sent = _run_recs(monkeypatch, recs=[{"title": "Zzzzzz Qqqq", "year": "2001", "reason": "r"}])
    assert status == 0 and len(sent) == 1
    subject, html = sent[0]
    assert "nothing new to recommend" in subject and not subject.startswith("⚠")
    assert "matched a film with upcoming showtimes" in html and "This run" not in html


def test_recs_unmatched_picks_still_flag_unreadable_theatres(monkeypatch):
    bad = {"state": "failed", "reason": "could not fetch the page (HTTP Error 404)"}
    status, sent = _run_recs(monkeypatch, recs=[{"title": "Zzzzzz Qqqq", "year": "2001", "reason": "r"}],
                             statuses={"Alpha Cinema": OK, "Beta Cinema": bad})
    assert status == 1 and sent[0][0].startswith("⚠")
    assert "Beta Cinema: could not fetch the page" in sent[0][1]


def test_recs_nothing_eligible_sends_a_short_note_and_is_not_a_failure(monkeypatch):
    status, sent = _run_recs(monkeypatch, films=[_showing("Dune", "2021")])
    assert status == 0 and len(sent) == 1
    subject, html = sent[0]
    assert "nothing new to recommend" in subject and not subject.startswith("⚠")
    assert "already on your watchlist or watched list" in html
    assert "This run" not in html and "No recommendations this month" not in html


def test_recs_nothing_eligible_still_flags_unreadable_theatres(monkeypatch):
    bad = {"state": "failed", "reason": "could not fetch the page (HTTP Error 404)"}
    status, sent = _run_recs(monkeypatch, films=[_showing("Dune", "2021")],
                             statuses={"Alpha Cinema": OK, "Beta Cinema": bad})
    assert status == 1 and len(sent) == 1
    assert sent[0][0].startswith("⚠") and "Beta Cinema: could not fetch the page" in sent[0][1]


def test_recs_nothing_eligible_send_failure_exits_nonzero(monkeypatch):
    status, _ = _run_recs(monkeypatch, films=[_showing("Dune", "2021")], send_result=False)
    assert status == 1


def test_recs_send_failure_exits_nonzero(monkeypatch):
    status, _ = _run_recs(monkeypatch, send_result=False)
    assert status == 1


# ── parse_relative_date: dates the old yearless parse couldn't read ───────────

@pytest.mark.parametrize("module", [wc, rc])
@pytest.mark.parametrize("today,text,expected", [
    (date(2028, 2, 20), "Feb 29", date(2028, 2, 29)),         # leap day
    (date(2027, 12, 20), "Feb 29", date(2028, 2, 29)),        # leap day, next year
    (date(2028, 2, 20), "Thu Feb 29", date(2028, 2, 29)),
    (date(2028, 2, 20), "Sept 4", date(2028, 9, 4)),          # "Sept" as well as "Sep"
    (date(2028, 2, 20), "SEPT 4", date(2028, 9, 4)),
    (date(2026, 3, 18), "Jan 17", date(2026, 1, 17)),         # exactly 60 days back stays this year
    (date(2026, 3, 18), "Jan 16", date(2027, 1, 16)),         # 61 days back rolls to next year
    (date(2026, 12, 20), "Jan 5", date(2027, 1, 5)),
])
def test_parse_relative_date_handles_leap_day_and_sept(module, today, text, expected):
    assert module.parse_relative_date(text, today) == expected


@pytest.mark.parametrize("module", [wc, rc])
def test_parse_relative_date_rejects_impossible_dates(module):
    assert module.parse_relative_date("Feb 29", date(2026, 2, 20)) is None   # 2026 and 2027 aren't leap years
    assert module.parse_relative_date("Smarch 40", date(2026, 3, 18)) is None
