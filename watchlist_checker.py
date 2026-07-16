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
from html import escape

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


# ── EMAIL STYLE — "Marquee" ticket/marquee visual identity ────────────────────
# Plain system fonts only (Georgia/Arial/Courier New) — no @font-face, since
# Gmail/Outlook strip embedded webfonts.
#
# Light mode only. Gmail's mobile app runs its own automatic dark-mode
# re-coloring pass over emails regardless of the <meta color-scheme> hint or
# an explicit @media (prefers-color-scheme: dark) block — an intentional dark
# palette was tried and abandoned because Gmail kept overriding specific
# elements (the board) back to dark anyway, so a maintained dark variant
# wasn't buying anything. Not worth the upkeep; this is light-only by design.
#
# NOTE: no CSS custom properties (var()) here — Gmail's mobile apps don't
# support them at all, which silently drops every color/background/border
# tied to a variable while structural CSS (flex, literal px, font-weight)
# survives. Every color below is a literal value.
#
# NOTE: no position:absolute layout (bulb frame) and no writing-mode/rotated
# text (ticket stub) — both rendered broken in real-world Gmail testing
# (bulbs collapsed into a stray inline blob; rotated stub text forced its
# flex sibling to an oversized height). Replaced with plain-flow rows and a
# simple horizontal bottom bar, which survive Gmail's rendering.
MARQUEE_CSS = """
  * { box-sizing: border-box; }
  body { margin:0; background:#d9c69a; font-family:Georgia,'Times New Roman',serif;
         padding:24px 12px; }
  .email { max-width:600px; margin:0 auto; background:#ecdcae;
           box-shadow:0 16px 44px rgba(28,21,18,0.25); }
  .proscenium { height:16px;
                background:repeating-linear-gradient(100deg, #8f0016 0 9px, #c20120 9px 18px); }
  .board-wrap { padding:30px 32px 8px; text-align:center;
                background:radial-gradient(ellipse 70% 100% at 50% 0%, rgba(232,165,48,0.30), transparent 70%); }
  .bulb-row { text-align:center; }
  .bulb-row.top { margin-bottom:12px; }
  .bulb-row.bottom { margin-top:12px; }
  .fbulb { display:inline-block; width:6px; height:6px; margin:0 4px; border-radius:50%; background:#e8a530;
           box-shadow:0 0 6px #e8a530, 0 0 2px #fff6dd inset; }
  .board { position:relative; display:inline-block; background:#faf6ec;
           background-image:repeating-linear-gradient(to bottom, transparent 0 13px, rgba(36,26,18,0.06) 13px 14px);
           border:3px solid #241a12; border-radius:3px; padding:18px 22px 16px;
           box-shadow:0 0 0 6px #e8a530; text-align:center; }
  .board-row { font-family:Arial,Helvetica,sans-serif; font-weight:900; font-size:38px; line-height:1;
               letter-spacing:-0.01em; color:#241a12; }
  .subhead { font-family:Arial,Helvetica,sans-serif; font-size:11px; font-weight:700;
             letter-spacing:0.08em; text-transform:uppercase; color:#8f0016;
             text-align:center; white-space:nowrap; margin:10px 0 0; }
  .subhead .star { color:#e8a530; margin:0 6px; }
  .subhead b { color:#241a12; font-style:normal; }
  .datestamp { text-align:center; font-size:12px; font-style:italic; color:#7c6c58; margin:10px 0 0; }
  .content { padding:8px 32px 30px; }
  .day-head { font-family:Arial,Helvetica,sans-serif; font-size:17px; font-weight:900;
              letter-spacing:0.03em; text-transform:uppercase; color:#c20120;
              text-shadow:1.5px 1.5px 0 rgba(0,0,0,0.25);
              border-bottom:4px double #c20120; padding-bottom:7px; margin:34px 0 18px; }
  /* Section title (one per email, e.g. "All Films Playing This Week") —
     reads as a level above the .day-head headings used in the watchlist
     sections above it. */
  .section-head { font-family:Arial,Helvetica,sans-serif; font-size:13px; font-weight:900;
                   letter-spacing:0.12em; text-transform:uppercase; color:#241a12;
                   text-align:center; border-top:2px solid #e8a530; border-bottom:2px solid #e8a530;
                   padding:8px 0; margin:40px 0 6px; }
  .verify-note { font-size:12px; font-style:italic; color:#7c6c58; margin:16px 0 10px; }
  .film-group { margin-bottom:8px; }
  .film-header { margin:20px 0 8px; }
  .film-title { color:#241a12; text-decoration:none; font-size:19px; font-weight:800; letter-spacing:0.01em; }
  .film-title.uncertain { color:#6f5c42; font-weight:700; }
  .film-year { font-family:Georgia,serif; color:#6f5c42; font-size:13px; margin-left:7px; }
  .wl-note { display:block; font-family:'Courier New',Courier,monospace; font-size:11px;
             color:#9c8a6c; margin-top:6px; }
  .tag { display:inline-block; font-family:'Courier New',Courier,monospace; text-transform:uppercase;
         letter-spacing:0.06em; font-size:9.5px; font-weight:700; color:#c20120;
         border:1px solid #c20120; padding:2px 7px; border-radius:3px; margin-left:8px; vertical-align:middle; }
  .tag.watched { color:#9c8a6c; border-color:#9c8a6c; }
  .ticket { position:relative; background:#faf3df;
            border:2px solid #241a12; border-radius:6px; box-shadow:0 6px 16px -4px rgba(28,21,18,0.25);
            overflow:hidden; margin-bottom:10px; }
  .ticket::before { content:""; position:absolute; top:0; left:0; right:0; height:5px; background:#c20120; }
  .ticket.uncertain::before { background:#e8a530; opacity:0.75; }
  .ticket.other::before { background:#e8a530; }
  .ticket-frame { position:absolute; inset:9px 6px 6px; border:1px dashed #d8c48f;
                  border-radius:4px; opacity:0.7; pointer-events:none; }
  .ticket-main { padding:14px 18px 8px; }
  .ticket-eyebrow { font-family:'Courier New',Courier,monospace; font-size:8px;
                    font-weight:700; letter-spacing:0.12em; text-transform:uppercase; color:#e8a530; }
  .ticket.uncertain .ticket-eyebrow { color:#9c8a6c; }
  .ticket-stars { font-size:7px; letter-spacing:4px; color:#e8a530; opacity:0.5; margin-left:6px; }
  .ticket.uncertain .ticket-stars { color:#9c8a6c; }
  .venue-name { display:block; font-family:Georgia,serif; font-weight:700; font-size:13.5px; color:#241a12;
                text-decoration:none; border-bottom:1px solid #9c8a6c; margin:6px 0 8px; }
  .ticket.uncertain .venue-name, .ticket.other .venue-name { color:#6f5c42; }
  .time { display:inline-block; font-family:'Courier New',Courier,monospace; font-weight:700; font-size:12px;
          color:#8f0016; background:rgba(194,1,32,0.10); border:1px solid rgba(194,1,32,0.4);
          border-radius:3px; padding:2px 7px; margin:0 5px 6px 0; }
  .ticket.uncertain .time, .ticket.other .time { color:#6f5c42; background:transparent; border-color:#9c8a6c; }
  .ticket-stub-bar { border-top:1px dashed #d8c48f; padding:5px 18px; text-align:right;
                     font-family:'Courier New',Courier,monospace; font-size:9px; letter-spacing:0.06em;
                     text-transform:uppercase; color:#9c8a6c; opacity:0.75; }
  .no-matches { color:#7c6c58; margin:16px 0; }
  .other-row { padding:6px 0; border-bottom:1px solid #d8c48f; }
  .other-title { font-family:Georgia,serif; font-weight:700; font-size:13.5px; color:#241a12; text-decoration:none; }
  .other-year { color:#7c6c58; font-size:11px; margin-left:5px; }
  .other-watched { color:#9c8a6c; font-size:10px; font-style:italic; margin-left:6px; }
  .other-venue { display:block; font-size:11.5px; color:#6f5c42; margin-top:2px; }
  .other-venue a { color:#8f0016; text-decoration:none; }
  .other-day { display:block; margin-top:1px; }
  .divider { position:relative; height:1px; background:#d8c48f; margin:30px 0 20px; }
  .divider::after { content:"\\25C6"; position:absolute; left:50%; top:50%; transform:translate(-50%,-50%);
                     background:#ecdcae; color:#e8a530; padding:0 12px; font-size:10px; }
  /* Plain solid-color elements, not a CSS gradient/clip-path shape — Gmail
     doesn't reliably render either (confirmed: clip-path was ignored
     entirely and the repeating-gradient rendered as two flat bars instead
     of a repeating stripe). Individual elements with solid backgrounds
     can't fail to render. */
  .popcorn-wrap { text-align:center; margin:0 0 12px; }
  .popcorn-stripe { display:inline-block; width:3px; height:22px; }
  .popcorn-stripe.red { background:#c20120; }
  .popcorn-stripe.white { background:#ffffff; border-left:1px solid #d8c48f; border-right:1px solid #d8c48f; }
  .footer-stars { text-align:center; font-size:8px; letter-spacing:6px; color:#e8a530; opacity:0.5; margin:0 0 10px; }
  .footer { font-size:11px; color:#7c6c58; line-height:1.7; text-align:center; }
  .footer a { color:#c20120; text-decoration:none; }
  .footer-fineprint { font-family:'Courier New',Courier,monospace; font-size:9px; letter-spacing:0.04em;
                       color:#9c8a6c; opacity:0.65; text-align:center; margin:10px 0 0; }
"""


def _render_ticket(f, variant, eyebrow, stub_label, tag_html=""):
    """Render one film as a header plus one ticket per theatre (variant:
    'confident' | 'uncertain' | 'other'). Splitting per-theatre keeps each
    ticket short — a single ticket holding every theatre's showtimes for a
    wide-release film stretched into an unrecognizable wall of pills."""
    title_class = "film-title" if variant == "confident" else "film-title uncertain"
    wl_note = (
        f'<span class="wl-note">→ matched from watchlist: "{escape(f["wl_title"])}"</span>'
        if variant == "uncertain" and f.get("wl_title") else ""
    )
    header = f"""
    <div class="film-header {variant}">
      <a href="{f['url']}" class="{title_class}">{escape(f['title'])}</a><span class="film-year">{escape(str(f['year']))}</span>{tag_html}
      {wl_note}
    </div>
    """
    tickets = "".join(
        f"""
        <div class="ticket {variant}">
          <div class="ticket-frame" aria-hidden="true"></div>
          <div class="ticket-main">
            <span class="ticket-eyebrow">{eyebrow}</span><span class="ticket-stars">★ ★ ★</span>
            <a href="{t['url']}" class="venue-name">{escape(t['name'])}</a>
            {"".join(f'<span class="time">{tm}</span>' for tm in t['times'])}
          </div>
          <div class="ticket-stub-bar">{stub_label} · No. {abs(hash(f['title'] + t['name'])) % 10000:04d}</div>
        </div>
        """
        for t in f["theatres"]
    )
    return f'<div class="film-group">{header}{tickets}</div>'


def _group_other_by_film(other_by_date):
    """Group the "all films playing" entries by film across all days —
    unlike group_by_film (used by the watchlist sections, which only ever
    sees one day at a time), this needs to keep each showtime's day label
    so a multi-day listing doesn't lose track of which day a time belongs to.

    Returns list of {title, year, url, watched,
        theatres: [{name, url, days: [{label, times}]}]}
    sorted alphabetically by title.
    """
    films = {}
    for date_str in sorted(other_by_date):
        d = datetime.strptime(date_str, "%Y-%m-%d")
        day_label = d.strftime("%a")  # e.g. "Thu"
        for e in other_by_date[date_str]:
            f = films.setdefault(e["title"], {
                "title": e["title"], "year": e["year"], "url": e["url"],
                "watched": e.get("watched", False), "theatres": {},
            })
            t = f["theatres"].setdefault(e["theatre"], {"url": e["theatre_url"], "days": {}})
            times = t["days"].setdefault(day_label, [])
            if e["time"] not in times:
                times.append(e["time"])

    result = []
    for f in films.values():
        # dict preserves insertion order, and days were inserted in
        # chronological order (outer loop above iterates sorted dates)
        theatres = [
            {
                "name": name,
                "url": info["url"],
                "days": [
                    {"label": label, "times": sorted(times, key=_time_sort_key)}
                    for label, times in info["days"].items()
                ],
            }
            for name, info in sorted(f["theatres"].items())
        ]
        result.append({**f, "theatres": theatres})
    return sorted(result, key=lambda f: f["title"])


def _render_other_row(f):
    """Compact plain-list row for the low-priority "all films playing" section
    — intentionally not ticket-styled: this is auxiliary info, not a watchlist
    match, so it shouldn't compete visually, and keeping it lightweight helps
    the whole email stay under Gmail's clipping size threshold."""
    watched_html = '<span class="other-watched">(watched)</span>' if f.get("watched") else ""
    venue_lines = "".join(
        f"""<span class="other-venue">
          <a href="{t['url']}">{escape(t['name'])}</a>
          {"".join(f'<span class="other-day">{d["label"]}: {", ".join(d["times"])}</span>' for d in t['days'])}
        </span>"""
        for t in f["theatres"]
    )
    return f"""
    <div class="other-row">
      <a href="{f['url']}" class="other-title">{escape(f['title'])}</a><span class="other-year">{escape(str(f['year']))}</span>{watched_html}
      {venue_lines}
    </div>
    """


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

    bulb_row = ('<span class="fbulb"></span>' * 9)
    html = f"""
    <html><head>
    <meta name="color-scheme" content="light">
    <meta name="supported-color-schemes" content="light">
    <style>{MARQUEE_CSS}</style></head><body>
    <div class="email">
      <div class="proscenium"></div>
      <div class="board-wrap">
        <div class="bulb-row top" aria-hidden="true">{bulb_row}</div>
        <div class="board">
          <div class="board-row">MARQUEE</div>
          <p class="subhead"><span class="star">★</span>{escape(LOCATION)} <b>Watchlist Digest</b><span class="star">★</span></p>
        </div>
        <div class="bulb-row bottom" aria-hidden="true">{bulb_row}</div>
        <p class="datestamp">{today_str}</p>
      </div>
      <div class="content">
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
                    tag_html = '<span class="tag">Rewatch</span>' if f.get("watched") else ""
                    html += _render_ticket(f, "confident", "Now Showing", "Admit One", tag_html)

            if day["uncertain"]:
                html += '<p class="verify-note">Possible matches — verify before going:</p>'
                for f in group_by_film(day["uncertain"]):
                    tag_html = '<span class="tag">Rewatch</span>' if f.get("watched") else ""
                    html += _render_ticket(f, "uncertain", "Possible Match", "Verify", tag_html)

    if other_by_date:
        # Grouped by movie, not by day (unlike the watchlist sections above) —
        # this section is just "what's playing," so each film appears once
        # with every theatre/day/time underneath, rather than being repeated
        # under a separate heading for each day.
        grouped_other = _group_other_by_film(other_by_date)
        if grouped_other:
            html += '<h2 class="section-head">All Films Playing This Week</h2>'
            for f in grouped_other:
                html += _render_other_row(f)

    theatre_list = " · ".join(
        f'<a href="{url}">{escape(name)}</a>'
        for name, url in theatre_urls.items()
    )
    html += f"""
        <div class="divider"></div>
        <div class="popcorn-wrap" aria-hidden="true"><span class="popcorn-stripe red"></span><span class="popcorn-stripe white"></span><span class="popcorn-stripe red"></span><span class="popcorn-stripe white"></span><span class="popcorn-stripe red"></span></div>
        <p class="footer-stars">★ ★ ★</p>
        <p class="footer">
          Theatres checked: {theatre_list}<br>
          Showtimes via CinemaClock. Verify at venue before going.
        </p>
        <p class="footer-fineprint">One digest per household · No refunds, exchanges, or regrets · Void where showtimes have changed</p>
      </div>
    </div>
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
    span = f"next {LOOKAHEAD_DAYS} days" if LOOKAHEAD_DAYS != 1 else "next day"
    subject = (
        f"🎬 Marquee: {total_matches} watchlist film(s) playing the {span} — {LOCATION}"
        if total_matches
        else f"🎬 Marquee: no watchlist matches the {span} — {LOCATION}"
    )
    html = build_email_html(dict(entries_by_date), THEATRES, dict(other_by_date))
    send_email(subject, html)

    print(f"\nDone. {total_matches} confirmed match(es) across all theatres.\n")


def register_scheduled_task():
    """Register/update Windows Task Scheduler entry for the watchlist digest.

    Runs on a fixed day interval (schedule_interval_days) rather than a
    fixed weekday, so the run cadence can be kept in step with
    lookahead_days — e.g. a 3-day lookahead paired with a 3-day interval
    means every showtime gets covered by some run, without long gaps.
    """
    import subprocess
    script    = os.path.abspath(__file__)
    python    = sys.executable
    interval  = int(_cfg["watchlist"].get("schedule_interval_days", 3))
    time_     = _cfg["watchlist"].get("schedule_time", "20:00")
    task_name = "WatchlistChecker"
    cmd = (
        f'schtasks /create /f '
        f'/tn "{task_name}" '
        f'/tr "\\"{python}\\" \\"{script}\\"" '
        f'/sc daily /mo {interval} /st {time_}'
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        print(f'✓ Task "{task_name}" registered — runs every {interval} day(s) at {time_}')
    else:
        print(f'✗ schtasks failed:\n{result.stderr.strip()}')
        print("  You may need to run this from an elevated (admin) prompt.")


if __name__ == "__main__":
    if "--schedule" in sys.argv:
        register_scheduled_task()
    else:
        main()
