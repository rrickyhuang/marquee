"""
Marquee — watchlist_checker.py
Scrapes theatre showtimes and emails a weekly digest of Letterboxd watchlist matches.

SETUP:
1. pip install -r requirements.txt
2. Copy config.example.yaml → config.yaml and fill in your values
3. Copy .env.example → .env and add your Gmail app password
4. Run manually: python watchlist_checker.py
5. Schedule weekly: `python watchlist_checker.py --schedule` (Windows Task Scheduler).
   On macOS/Linux, use cron to run the script directly instead.

GMAIL APP PASSWORD:
  Google Account → Security → 2-Step Verification → App passwords
  Generate one for "Mail" — paste the 16-char string in .env.
"""

import csv
import glob
import io
import os
import re
import smtplib
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser

import yaml

# Windows terminals often default to cp1252; force UTF-8 output
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ────────────────────────────────────────────────────────────────────

_script_dir = os.path.dirname(os.path.abspath(__file__))

# Load .env from the same directory as this script (no extra packages needed)
_env_path = os.path.join(_script_dir, ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())


def load_config():
    config_path = os.path.join(_script_dir, "config.yaml")
    try:
        with open(config_path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(
            "config.yaml not found. Copy config.example.yaml to config.yaml and fill in your values."
        )


_cfg = load_config()

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS") or _cfg["email"]["from"]
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL       = _cfg["email"]["to"]

MATCH_THRESHOLD = _cfg["matching"]["threshold"]
UNCERTAIN_FLOOR = _cfg["matching"]["uncertain_floor"]
LOOKAHEAD_DAYS  = _cfg["watchlist"]["lookahead_days"]
LOCATION        = _cfg.get("location", "Local Theatres")

THEATRES, THEATRE_HOMEPAGES = {}, {}
for _t in _cfg["theatres"]:
    THEATRES[_t["name"]] = _t["cinemaclock_url"]
    THEATRE_HOMEPAGES[_t["name"]] = _t["homepage"]

LETTERBOXD_RSS          = f"https://letterboxd.com/{_cfg['letterboxd']['username']}/watchlist/rss/"
LETTERBOXD_ACTIVITY_RSS = f"https://letterboxd.com/{_cfg['letterboxd']['username']}/rss/"


# ── LETTERBOXD EXPORT ─────────────────────────────────────────────────────────

def load_letterboxd_export():
    """Find the most recent Letterboxd export ZIP in the project folder.

    Returns (watched_norms, watchlist_entries) where:
      watched_norms:      set of normalized titles from watched.csv
      watchlist_entries:  list of (title, year, lb_url) from watchlist.csv
    Returns (set(), []) if no ZIP is found or readable.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    zips = glob.glob(os.path.join(script_dir, "*.zip"))
    if not zips:
        return set(), []

    zip_path = max(zips, key=os.path.getmtime)
    print(f"  Export ZIP: {os.path.basename(zip_path)}")

    watched_norms = set()
    watchlist_entries = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            if "watched.csv" in names:
                with z.open("watched.csv") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                        if row.get("Name"):
                            watched_norms.add(normalize(row["Name"]))
            if "watchlist.csv" in names:
                with z.open("watchlist.csv") as f:
                    for row in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")):
                        if row.get("Name"):
                            watchlist_entries.append((
                                row["Name"],
                                row.get("Year", ""),
                                row.get("Letterboxd URI", ""),
                            ))
    except Exception as e:
        print(f"  Warning: could not read export ZIP ({e})")

    return watched_norms, watchlist_entries


# ── RSS ────────────────────────────────────────────────────────────────────────

def fetch_watchlist_rss(rss_url=LETTERBOXD_RSS):
    """Build watchlist from the export ZIP, patched with the watchlist RSS.

    The ZIP is the primary source (complete history). The watchlist RSS patches
    in any films added since the last export. Note: Letterboxd's watchlist RSS
    is currently blocked by Cloudflare (403), so in practice the ZIP is the
    sole source — drop a fresh export ZIP periodically to stay current.
    """
    print(f"Loading watchlist...")
    rss_items = []
    try:
        xml = fetch_html(rss_url)
        for item_m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
            item = item_m.group(1)

            # <title><![CDATA[Film Title (YEAR)]]></title>
            title_m = re.search(
                r"<title>(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?</title>", item, re.DOTALL
            )
            # <link> appears right after </title> in Letterboxd RSS
            link_m = re.search(r"<link>\s*(https?://[^\s<]+)\s*</link>", item)
            if not title_m:
                continue

            raw = title_m.group(1).strip()
            lb_url = link_m.group(1).strip() if link_m else ""

            # Strip trailing " (YEAR)" if present
            year_m = re.search(r"\s*\((\d{4})\)\s*$", raw)
            if year_m:
                year = year_m.group(1)
                title = raw[: year_m.start()].strip()
            else:
                year = ""
                title = raw

            if title:
                rss_items.append((title, year, lb_url))

        print(f"  {len(rss_items)} films from watchlist RSS")
    except Exception as e:
        print(f"  Watchlist RSS unavailable ({e}) — using ZIP only")

    _, zip_entries = load_letterboxd_export()

    if not rss_items and not zip_entries:
        print("  Warning: no ZIP found and watchlist RSS unavailable — watchlist will be empty")
        return []

    if not rss_items:
        print(f"  {len(zip_entries)} films from ZIP")
        return zip_entries

    if not zip_entries:
        print(f"  No export ZIP found — using RSS only ({len(rss_items)} films)")
        return rss_items

    # Merge: ZIP as foundation (complete history), RSS patches in recent additions
    zip_norms = {normalize(t) for t, _, _ in zip_entries}
    merged = list(zip_entries)
    added = 0
    for title, year, lb_url in rss_items:
        if normalize(title) not in zip_norms:
            merged.append((title, year, lb_url))
            added += 1

    print(f"  Merged: {len(zip_entries)} ZIP + {added} RSS-only = {len(merged)} total")
    return merged


def load_watched_titles():
    """Return a set of normalized watched titles.

    ZIP is the primary source (full history). The activity RSS
    (diary entries at /username/rss/) patches in recent watches not yet
    exported. This RSS feed is distinct from the watchlist RSS and is not
    Cloudflare-blocked.
    """
    watched_norms, _ = load_letterboxd_export()
    try:
        xml = fetch_html(LETTERBOXD_ACTIVITY_RSS)
        for item_m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
            item = item_m.group(1)
            title_m = re.search(r"<letterboxd:filmTitle>(.*?)</letterboxd:filmTitle>", item)
            if title_m:
                watched_norms.add(normalize(title_m.group(1).strip()))
    except Exception as e:
        print(f"  Warning: could not fetch activity RSS ({e}); using ZIP only")
    return watched_norms


# ── LETTERBOXD URL ────────────────────────────────────────────────────────────

def slugify(title):
    """Convert a film title to a Letterboxd-style URL slug."""
    t = title.lower()
    t = re.sub(r"[^\w\s-]", "", t)
    t = re.sub(r"[\s_]+", "-", t)
    t = re.sub(r"-+", "-", t)
    return t.strip("-")


def lb_film_url(title, year=""):
    """Slug-based Letterboxd film URL, falling back to a search URL."""
    import urllib.parse
    slug = slugify(title)
    if slug:
        return f"https://letterboxd.com/film/{slug}/"
    query = urllib.parse.quote_plus(f"{title} {year}".strip())
    return f"https://letterboxd.com/search/{query}/"


# ── SCRAPING ──────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_relative_date(text, today):
    """Convert CinemaClock date strings to a date object. Returns None if unparseable.

    Handles: "Today", "Tomorrow", "Fri Mar 21", "Friday March 21", etc.
    """
    text = text.strip()
    lower = text.lower()
    if lower == "today":
        return today
    if lower == "tomorrow":
        return today + timedelta(days=1)
    # Try various abbreviated/full formats like "Mar 18", "Fri Mar 21", "Friday March 21"
    for fmt in ("%b %d", "%B %d", "%a %b %d", "%A %b %d", "%a %B %d", "%A %B %d"):
        try:
            d = datetime.strptime(text, fmt)
            candidate = d.replace(year=today.year).date()
            # If the date is more than 60 days in the past, assume it rolled to next year
            if (candidate - today).days < -60:
                candidate = d.replace(year=today.year + 1).date()
            return candidate
        except ValueError:
            continue
    return None


def fmt_time(data_time_str):
    """Convert CinemaClock data-time='HHMM' (24h) to '9:30pm' display string."""
    t = int(data_time_str)
    h, m = t // 100, t % 100
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}"


def extract_dated_showtimes(block, today):
    """Scan a movie's HTML block and return list of {date, time} dicts.

    CinemaClock structure:
        <u>Thu <span class="timesdate">Mar 19</span></u>
        <i><span class="tix" data-time="1130">11:30am </span> ...</i>
    """
    showtimes = []
    current_date = None

    # Match either a timesdate span (date like "Mar 18") or a tix/notix span (data-time="HHMM")
    token_re = re.compile(
        r'class=["\']timesdate["\'][^>]*>\s*([^<]+?)\s*<'
        r'|class=["\'](?:tix|notix)[^"\']*["\'][^>]*\bdata-time=["\'](\d{3,4})["\']',
        re.IGNORECASE,
    )

    for m in token_re.finditer(block):
        date_text, data_time = m.group(1), m.group(2)
        if date_text:
            parsed = parse_relative_date(date_text.strip(), today)
            if parsed:
                current_date = parsed
        elif data_time:
            if current_date is not None:
                showtimes.append({"date": current_date.isoformat(), "time": fmt_time(data_time)})
            else:
                # No date header seen yet — fall back to today
                showtimes.append({"date": today.isoformat(), "time": fmt_time(data_time)})

    return showtimes


def scrape_cinemaclock(url):
    """Return list of {title, year, showtimes, url} dicts from a CinemaClock theatre page.

    showtimes is a list of {date: 'YYYY-MM-DD', time: 'H:MM'} dicts.
    """
    try:
        today = date.today()
        html = fetch_html(url)
        films = []
        blocks = re.split(r'(?=<div[^>]+class="showtimeblock movie)', html)
        for block in blocks:
            # Title: <h3 class='movietitle ...'><a href='/movies/slug'>TITLE</a></h3>
            title_m = re.search(
                r"<h3[^>]+class=['\"]movietitle[^'\"]*['\"][^>]*>"
                r".*?<a[^>]*href=['\"]([^'\"]+)['\"][^>]*>([^<]+)</a></h3>",
                block, re.DOTALL,
            )
            if not title_m:
                continue
            movie_path, title = title_m.group(1), title_m.group(2).strip()
            if not title:
                continue
            movie_url = f"https://www.cinemaclock.com{movie_path}"

            # Year: first 4-digit year in the moviegenre paragraph
            year_m = re.search(
                r"class=['\"]moviegenre['\"][^>]*>(.*?)</p>", block, re.DOTALL
            )
            year = ""
            if year_m:
                yr = re.search(r"\b((?:19|20)\d{2})\b", year_m.group(1))
                year = yr.group(1) if yr else ""

            showtimes = extract_dated_showtimes(block, today)
            films.append({
                "title": title,
                "year": year,
                "showtimes": showtimes,
                "url": movie_url,
            })
        return films
    except Exception as e:
        print(f"  Error scraping {url}: {e}")
        return []


# ── MATCHING ──────────────────────────────────────────────────────────────────

def normalize(title):
    """Lowercase, strip articles, remove punctuation for comparison."""
    t = title.lower()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def match_against_watchlist(scraped_films, watchlist):
    """watchlist items are (title, year, lb_url) triples."""
    matches = []
    uncertain = []
    for film in scraped_films:
        best_score = 0
        best_wl = None
        for entry in watchlist:
            wl_title = entry[0]
            score = similarity(film["title"], wl_title)
            if score > best_score:
                best_score = score
                best_wl = entry
        if best_score >= MATCH_THRESHOLD:
            matches.append({**film, "wl_title": best_wl[0], "lb_url": best_wl[2] if len(best_wl) > 2 else "", "score": best_score})
        elif best_score >= UNCERTAIN_FLOOR:
            uncertain.append({**film, "wl_title": best_wl[0], "lb_url": best_wl[2] if len(best_wl) > 2 else "", "score": best_score})
    return matches, uncertain


# ── EMAIL ─────────────────────────────────────────────────────────────────────

def _time_sort_key(time_str):
    """Return a (hour24, minute) tuple for sorting '9:30pm', '11:00am', etc."""
    m = re.match(r"(\d+):(\d+)(am|pm)", time_str)
    if not m:
        return (0, 0)
    h, mn, suffix = int(m.group(1)), int(m.group(2)), m.group(3)
    if suffix == "pm" and h != 12:
        h += 12
    elif suffix == "am" and h == 12:
        h = 0
    return (h, mn)


def group_by_film(entries):
    """Collapse flat [{title, theatre, time, ...}] into per-film groups.

    Returns list of:
        {title, year, wl_title, url,
         theatres: [{name, url, times: [str, ...]}, ...]}
    sorted alphabetically by title.
    """
    films = {}
    for e in entries:
        f = films.setdefault(e["title"], {
            "title": e["title"],
            "year": e["year"],
            "wl_title": e.get("wl_title", ""),
            "url": e["url"],
            "watched": e.get("watched", False),
            "theatres": {},
        })
        t = f["theatres"].setdefault(e["theatre"], {"url": e["theatre_url"], "times": []})
        t["times"].append(e["time"])

    result = []
    for f in films.values():
        theatres = [
            {"name": name, "url": info["url"],
             "times": sorted(info["times"], key=_time_sort_key)}
            for name, info in sorted(f["theatres"].items())
        ]
        result.append({**f, "theatres": theatres})
    return sorted(result, key=lambda f: f["title"])


def build_email_html(entries_by_date, theatre_urls, other_by_date=None):
    """Build HTML email grouped by day.

    entries_by_date: {
        'YYYY-MM-DD': {
            'confident': [{title, year, wl_title, theatre, theatre_url, time, url}, ...],
            'uncertain': [...],
        }
    }
    other_by_date: {
        'YYYY-MM-DD': [{title, year, theatre, theatre_url, time, url}, ...]
    }
    """
    today_str = datetime.now().strftime("%B %d, %Y")
    has_any = any(
        day["confident"] or day["uncertain"]
        for day in entries_by_date.values()
    )

    html = f"""
    <html><head><style>
      body {{ font-family:Georgia,serif; max-width:600px; margin:0 auto;
              background:#fff; color:#1a1a1a; padding:24px; }}
      .hdr {{ font-size:16px; letter-spacing:0.1em; text-transform:uppercase;
              color:#666; border-bottom:1px solid #ddd; padding-bottom:8px; }}
      .hdr-date {{ font-size:12px; font-weight:normal; }}
      .no-matches {{ color:#666; margin-top:16px; }}
      .day-head {{ font-size:14px; text-transform:uppercase; letter-spacing:0.08em;
                   color:#c97c3a; margin-top:28px; margin-bottom:10px;
                   border-bottom:1px solid #f0e0d0; padding-bottom:6px; }}
      .day-head-other {{ font-size:13px; text-transform:uppercase; letter-spacing:0.08em;
                         color:#999; margin-top:20px; margin-bottom:8px;
                         border-bottom:1px solid #eee; padding-bottom:4px; }}
      .verify-note {{ font-size:11px; color:#999; margin:10px 0 4px; }}
      .card {{ padding:8px 12px; margin-bottom:8px; }}
      .card-confident {{ border-left:3px solid #c97c3a; background:#fdf8f3; }}
      .card-uncertain {{ border-left:3px solid #ddd; background:#f9f9f9; padding:6px 12px; margin-bottom:6px; }}
      .card-other {{ border-left:3px solid #ddd; background:#fafafa; padding:7px 12px; margin-bottom:6px; }}
      .title-confident {{ color:#1a1a1a; text-decoration:none; }}
      .title-uncertain {{ color:#777; text-decoration:none; }}
      .title-other {{ color:#555; text-decoration:none; font-size:14px; }}
      .year-confident {{ color:#888; font-size:12px; margin-left:6px; }}
      .year-uncertain {{ color:#bbb; font-size:11px; margin-left:4px; }}
      .year-other {{ color:#bbb; font-size:12px; margin-left:6px; }}
      .venue-confident {{ display:block; font-size:12px; color:#555; margin-top:3px; }}
      .venue-uncertain {{ display:block; font-size:11px; color:#999; margin-top:3px; }}
      .venue-other {{ display:block; font-size:12px; color:#999; margin-top:3px; }}
      .venue-link-confident {{ color:#c97c3a; text-decoration:none; }}
      .venue-link-uncertain {{ color:#bbb; text-decoration:none; }}
      .venue-link-other {{ color:#aaa; text-decoration:none; }}
      .tag {{ display:inline-block; letter-spacing:0.05em; text-transform:uppercase;
              color:#fff; border-radius:3px; vertical-align:middle; }}
      .tag-rewatch {{ font-size:10px; background:#c97c3a; padding:1px 6px; margin-left:8px; }}
      .tag-rewatch-uncertain {{ font-size:9px; background:#bbb; padding:1px 5px; margin-left:6px; }}
      .tag-watched {{ font-size:10px; background:#9c8455; padding:1px 6px; margin-left:8px; }}
      .footer {{ font-size:11px; color:#aaa; margin-top:32px; border-top:1px solid #eee; padding-top:12px; }}
      .footer a {{ color:#aaa; }}
    </style></head><body>
    <h2 class="hdr">
      {LOCATION} — Watchlist Digest<br>
      <span class="hdr-date">{today_str}</span>
    </h2>
    """

    if not has_any:
        html += '<p class="no-matches">No watchlist matches this week.</p>'
    else:
        for date_str in sorted(entries_by_date):
            day = entries_by_date[date_str]
            if not day["confident"] and not day["uncertain"]:
                continue

            # Format day heading: "Wednesday, March 18"
            d = datetime.strptime(date_str, "%Y-%m-%d")
            day_label = d.strftime("%A, %B %-d") if sys.platform != "win32" else d.strftime("%A, %B {d}").replace("{d}", str(d.day))

            html += f'<h3 class="day-head">{day_label}</h3>'

            if day["confident"]:
                for f in group_by_film(day["confident"]):
                    venue_lines = "".join(
                        f"""<span class="venue-confident">
                          <a href="{t['url']}" class="venue-link-confident">{t['name']}</a>
                          &nbsp;·&nbsp; {"&ensp;".join(t['times'])}
                        </span>"""
                        for t in f["theatres"]
                    )
                    rewatch_tag = (
                        '<span class="tag tag-rewatch">Rewatch</span>'
                        if f.get("watched") else ""
                    )
                    html += f"""
                    <div class="card card-confident">
                      <strong><a href="{f['url']}" class="title-confident">{f['title']}</a></strong>
                      <span class="year-confident">{f['year']}</span>{rewatch_tag}
                      {venue_lines}
                    </div>
                    """

            if day["uncertain"]:
                html += '<p class="verify-note">Possible matches (verify):</p>'
                for f in group_by_film(day["uncertain"]):
                    venue_lines = "".join(
                        f"""<span class="venue-uncertain">
                          <a href="{t['url']}" class="venue-link-uncertain">{t['name']}</a>
                          &nbsp;·&nbsp; {"&ensp;".join(t['times'])}
                        </span>"""
                        for t in f["theatres"]
                    )
                    rewatch_tag = (
                        '<span class="tag tag-rewatch-uncertain">Rewatch</span>'
                        if f.get("watched") else ""
                    )
                    html += f"""
                    <div class="card card-uncertain">
                      <a href="{f['url']}" class="title-uncertain">{f['title']}</a>
                      <span class="year-uncertain">{f['year']}</span>
                      → <em>{f['wl_title']}</em>{rewatch_tag}
                      {venue_lines}
                    </div>
                    """

    if other_by_date:
        html += '<h2 class="hdr" style="color:#888;margin-top:40px;border-top:2px solid #eee;padding-top:20px;">All Films Playing This Week</h2>'
        for date_str in sorted(other_by_date):
            entries = other_by_date[date_str]
            if not entries:
                continue
            d = datetime.strptime(date_str, "%Y-%m-%d")
            day_label = d.strftime("%A, %B %-d") if sys.platform != "win32" else d.strftime("%A, %B {d}").replace("{d}", str(d.day))
            html += f'<h3 class="day-head-other">{day_label}</h3>'
            for f in group_by_film(entries):
                venue_lines = "".join(
                    f"""<span class="venue-other">
                      <a href="{t['url']}" class="venue-link-other">{t['name']}</a>
                      &nbsp;·&nbsp; {"&ensp;".join(t['times'])}
                    </span>"""
                    for t in f["theatres"]
                )
                watched_tag = (
                    '<span class="tag tag-watched">Watched</span>'
                    if f.get("watched") else ""
                )
                html += f"""
                <div class="card card-other">
                  <a href="{f['url']}" class="title-other">{f['title']}</a>
                  <span class="year-other">{f['year']}</span>{watched_tag}
                  {venue_lines}
                </div>
                """

    theatre_list = " · ".join(
        f'<a href="{url}">{name}</a>'
        for name, url in theatre_urls.items()
    )
    html += f"""
    <p class="footer">
      Theatres checked: {theatre_list}<br>
      Showtimes via CinemaClock. Verify at venue before going.
    </p>
    </body></html>
    """
    return html


def send_email(subject, html_body):
    if not GMAIL_APP_PASSWORD:
        print("Email not configured. Set GMAIL_APP_PASSWORD in .env.")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = NOTIFY_EMAIL
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_ADDRESS, NOTIFY_EMAIL, msg.as_string())
        print(f"✓ Email sent to {NOTIFY_EMAIL}")
        return True
    except Exception as e:
        print(f"✗ Email failed: {e}")
        return False


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"Marquee — watchlist digest — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")

    today = date.today()
    window_end = today + timedelta(days=LOOKAHEAD_DAYS - 1)  # lookahead_days inclusive

    # entries_by_date[date_str] = {"confident": [...], "uncertain": [...]}
    entries_by_date = defaultdict(lambda: {"confident": [], "uncertain": []})
    seen_showtimes: set[tuple] = set()  # (title, theatre, date, time) dedup key
    other_by_date = defaultdict(list)   # date_str → [{title, year, theatre, theatre_url, time, url}, ...]
    seen_other_showtimes: set[tuple] = set()
    total_matches = 0

    print("\nFetching watched titles...")
    watched_norms = load_watched_titles()
    print(f"  {len(watched_norms)} watched films (ZIP + activity RSS)")

    watchlist = fetch_watchlist_rss()

    for theatre_name, url in THEATRES.items():
        print(f"\nScraping: {theatre_name}...")
        scraped = scrape_cinemaclock(url)
        print(f"  Found {len(scraped)} films listed")

        matches, uncertain = match_against_watchlist(scraped, watchlist)
        total_matches += len(matches)

        if matches:
            print(f"  ✓ {len(matches)} watchlist match(es):")
            for m in matches:
                showtimes_within = [
                    st for st in m["showtimes"]
                    if today <= date.fromisoformat(st["date"]) <= window_end
                ]
                dates_summary = ", ".join(
                    f"{st['date']} {st['time']}" for st in showtimes_within
                ) or f"dates outside {LOOKAHEAD_DAYS}-day window"
                print(f"    — {m['title']} ({m['year']})  [{dates_summary}]")
                for st in showtimes_within:
                    key = (m["title"], theatre_name, st["date"], st["time"])
                    if key in seen_showtimes:
                        continue
                    seen_showtimes.add(key)
                    entries_by_date[st["date"]]["confident"].append({
                        "title": m["title"],
                        "year": m["year"],
                        "wl_title": m["wl_title"],
                        "score": m["score"],
                        "theatre": theatre_name,
                        "theatre_url": THEATRE_HOMEPAGES.get(theatre_name, url),
                        "time": st["time"],
                        "url": m.get("lb_url") or lb_film_url(m["title"], m.get("year", "")),
                        "watched": normalize(m["title"]) in watched_norms,
                    })

        if uncertain:
            print(f"  ~ {len(uncertain)} uncertain match(es) (check manually)")
            for m in uncertain:
                for st in m["showtimes"]:
                    if today <= date.fromisoformat(st["date"]) <= window_end:
                        key = (m["title"], theatre_name, st["date"], st["time"])
                        if key in seen_showtimes:
                            continue
                        seen_showtimes.add(key)
                        entries_by_date[st["date"]]["uncertain"].append({
                            "title": m["title"],
                            "year": m["year"],
                            "wl_title": m["wl_title"],
                            "score": m["score"],
                            "theatre": theatre_name,
                            "theatre_url": THEATRE_HOMEPAGES.get(theatre_name, url),
                            "time": st["time"],
                            "url": m.get("lb_url") or lb_film_url(m["title"], m.get("year", "")),
                            "watched": normalize(m["title"]) in watched_norms,
                        })

        if not matches and not uncertain:
            print("  No matches")

        matched_norms = {normalize(m["title"]) for m in matches + uncertain}
        non_matches = [f for f in scraped if normalize(f["title"]) not in matched_norms]
        for film in non_matches:
            for st in film["showtimes"]:
                if today <= date.fromisoformat(st["date"]) <= window_end:
                    key = (film["title"], theatre_name, st["date"], st["time"])
                    if key in seen_other_showtimes:
                        continue
                    seen_other_showtimes.add(key)
                    other_by_date[st["date"]].append({
                        "title": film["title"],
                        "year": film["year"],
                        "theatre": theatre_name,
                        "theatre_url": THEATRE_HOMEPAGES.get(theatre_name, url),
                        "time": st["time"],
                        "url": lb_film_url(film["title"], film.get("year", "")),
                        "watched": normalize(film["title"]) in watched_norms,
                    })

    # Console summary grouped by day
    print(f"\n{'─'*50}")
    print("MATCHES BY DAY")
    print(f"{'─'*50}")
    if not entries_by_date:
        print(f"No matches in the next {LOOKAHEAD_DAYS} days.")
    else:
        for date_str in sorted(entries_by_date):
            day = entries_by_date[date_str]
            if not day["confident"] and not day["uncertain"]:
                continue
            d = datetime.strptime(date_str, "%Y-%m-%d")
            print(f"\n  {d.strftime('%A %b %-d') if sys.platform != 'win32' else d.strftime('%A %b {d}').replace('{d}', str(d.day))}")
            for f in group_by_film(day["confident"]):
                print(f"    ✓ {f['title']} ({f['year']})")
                for t in f["theatres"]:
                    print(f"        {t['name']}: {', '.join(t['times'])}")
            for f in group_by_film(day["uncertain"]):
                print(f"    ~ {f['title']} → {f['wl_title']}")
                for t in f["theatres"]:
                    print(f"        {t['name']}: {', '.join(t['times'])}")

    # Build and send email
    subject = (
        f"🎬 {total_matches} watchlist film(s) playing this week — {LOCATION} theatres"
        if total_matches
        else f"{LOCATION} theatres — no watchlist matches this week"
    )
    html = build_email_html(dict(entries_by_date), THEATRES, dict(other_by_date))
    send_email(subject, html)

    print(f"\nDone. {total_matches} confirmed match(es) across all theatres.\n")


def register_scheduled_task():
    """Register/update Windows Task Scheduler entry for the weekly watchlist digest."""
    import subprocess
    script    = os.path.abspath(__file__)
    python    = sys.executable
    day       = _cfg["watchlist"].get("schedule_day", "sunday").upper()[:3]  # e.g. "SUN"
    time_     = _cfg["watchlist"].get("schedule_time", "20:00")
    task_name = "WatchlistChecker"
    cmd = (
        f'schtasks /create /f '
        f'/tn "{task_name}" '
        f'/tr "\\"{python}\\" \\"{script}\\"" '
        f'/sc weekly /d {day} /st {time_}'
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        print(f'✓ Task "{task_name}" registered — runs every {day} at {time_}')
    else:
        print(f'✗ schtasks failed:\n{result.stderr.strip()}')
        print("  You may need to run this from an elevated (admin) prompt.")


if __name__ == "__main__":
    if "--schedule" in sys.argv:
        register_scheduled_task()
    else:
        main()
