"""Tests that dynamic strings are HTML-escaped in email builders (issue: XSS/broken markup)."""
import watchlist_checker
import recommendations


def test_watchlist_email_escapes_ampersand_title():
    entries_by_date = {
        "2026-07-10": {
            "confident": [{
                "title": "Fast & Furious",
                "year": "2001",
                "wl_title": "Fast & Furious",
                "url": "https://letterboxd.com/film/fast-furious/",
                "theatre": "AMC & Sons",
                "theatre_url": "https://example.com",
                "time": "7:00 PM",
            }],
            "uncertain": [],
        }
    }
    theatre_urls = {"AMC & Sons": "https://example.com"}
    html_out = watchlist_checker.build_email_html(entries_by_date, theatre_urls)

    assert "Fast &amp; Furious" in html_out
    assert "AMC &amp; Sons" in html_out
    assert "Fast & Furious" not in html_out


def test_recommendations_email_escapes_ampersand_title():
    recs_by_date = {
        "2026-07-10": [{
            "title": "Alien³",
            "year": "1992",
            "url": "https://letterboxd.com/film/alien-3/",
            "reason": "Because you liked <Aliens>",
            "theatres": [{"name": "AMC & Sons", "url": "https://example.com", "times": ["7:00 PM"]}],
        }]
    }
    html_out = recommendations.build_reco_email(recs_by_date)

    assert "AMC &amp; Sons" in html_out
    assert "Because you liked &lt;Aliens&gt;" in html_out
    assert "<Aliens>" not in html_out
