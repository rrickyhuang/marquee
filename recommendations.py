"""
Marquee — recommendations.py
Scrapes theatres, excludes already-watched and watchlisted films,
asks Claude to pick films based on your taste profile, and emails
a styled HTML digest.

SETUP:
1. pip install -r requirements.txt
2. Copy config.example.yaml → config.yaml and fill in your values
3. Copy .env.example → .env and add ANTHROPIC_API_KEY and GMAIL_APP_PASSWORD
4. Run manually:   python recommendations.py
5. Register task:  python recommendations.py --schedule
   (Windows Task Scheduler; day(s)/time come from recommendations.schedule_* in
   config.yaml. On macOS/Linux, use cron to run the script directly instead.)
"""

import json
import os
import csv
import glob
import io
import re
import smtplib
import sys
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yaml

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── CONFIG ─────────────────────────────────────────────────────────────────────

_script_dir = os.path.dirname(os.path.abspath(__file__))

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
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

EXCLUDE_THRESHOLD    = _cfg["matching"]["exclude_threshold"]
RECOMMENDATION_SCORE = _cfg["matching"]["recommendation_score"]
LOCATION             = _cfg.get("location", "Local Theatres")

LETTERBOXD_WATCHLIST_RSS = f"https://letterboxd.com/{_cfg['letterboxd']['username']}/watchlist/rss/"

THEATRES, THEATRE_HOMEPAGES = {}, {}
for _t in _cfg["theatres"]:
    THEATRES[_t["name"]] = _t["cinemaclock_url"]
    THEATRE_HOMEPAGES[_t["name"]] = _t["homepage"]

# Build taste profile from config
_tp           = _cfg["taste_profile"]
_five_stars   = ", ".join(_tp.get("five_star_films", []))
_liked        = ", ".join(_tp.get("liked_films", []))
_username     = _cfg["letterboxd"]["username"]
_loc_suffix   = f" — {LOCATION}" if LOCATION else ""

TASTE_PROFILE = f"""\
Letterboxd: @{_username}{_loc_suffix}.

{_tp.get("description", "").strip()}

5-star films: {_five_stars}

Liked (hearted): {_liked}\
"""

# ── HTTP ───────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


# ── NORMALISATION / SIMILARITY ─────────────────────────────────────────────────

def normalize(title):
    t = title.lower()
    t = re.sub(r"^(the|a|an)\s+", "", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


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

def fetch_watched_titles(rss_url=None):
    """Return a set of normalized watched titles.

    ZIP is the primary source (full history). The activity RSS
    (diary entries at /username/rss/) patches in recent watches not yet
    exported. This RSS feed is distinct from the watchlist RSS and is not
    Cloudflare-blocked.
    """
    if rss_url is None:
        rss_url = f"https://letterboxd.com/{_cfg['letterboxd']['username']}/rss/"
    # Full history from ZIP
    watched_norms, _ = load_letterboxd_export()

    # Recent watches from live RSS (adds anything logged since last export)
    try:
        xml = fetch_html(rss_url)
        for item_m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
            item = item_m.group(1)
            title_m = re.search(r"<letterboxd:filmTitle>(.*?)</letterboxd:filmTitle>", item)
            if title_m:
                watched_norms.add(normalize(title_m.group(1).strip()))
    except Exception as e:
        print(f"  Warning: could not fetch master RSS ({e}); using ZIP only")

    return watched_norms


# ── SCRAPING ───────────────────────────────────────────────────────────────────

def parse_relative_date(text, today):
    text = text.strip()
    lower = text.lower()
    if lower == "today":
        return today
    if lower == "tomorrow":
        return today + timedelta(days=1)
    for fmt in ("%b %d", "%B %d", "%a %b %d", "%A %b %d", "%a %B %d", "%A %B %d"):
        try:
            d = datetime.strptime(text, fmt)
            candidate = d.replace(year=today.year).date()
            if (candidate - today).days < -60:
                candidate = d.replace(year=today.year + 1).date()
            return candidate
        except ValueError:
            continue
    return None


def fmt_time(data_time_str):
    t = int(data_time_str)
    h, m = t // 100, t % 100
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{suffix}"


def extract_dated_showtimes(block, today):
    showtimes = []
    current_date = None
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
            d = current_date if current_date is not None else today
            showtimes.append({"date": d.isoformat(), "time": fmt_time(data_time)})
    return showtimes


def scrape_cinemaclock(url):
    try:
        today = date.today()
        html = fetch_html(url)
        films = []
        for block in re.split(r'(?=<div[^>]+class="showtimeblock movie)', html):
            title_m = re.search(
                r"<h3[^>]+class=['\"]movietitle[^'\"]*['\"][^>]*>"
                r".*?<a[^>]*href=['\"]([^'\"]+)['\"][^>]*>([^<]+)</a></h3>",
                block, re.DOTALL,
            )
            if not title_m:
                continue
            _, title = title_m.group(1), title_m.group(2).strip()
            if not title:
                continue
            year_m = re.search(r"class=['\"]moviegenre['\"][^>]*>(.*?)</p>", block, re.DOTALL)
            year = ""
            if year_m:
                yr = re.search(r"\b((?:19|20)\d{2})\b", year_m.group(1))
                year = yr.group(1) if yr else ""
            films.append({
                "title": title,
                "year": year,
                "showtimes": extract_dated_showtimes(block, today),
            })
        return films
    except Exception as e:
        print(f"  Error scraping {url}: {e}")
        return []


# ── LETTERBOXD URL ─────────────────────────────────────────────────────────────

def slugify(title):
    t = title.lower()
    t = re.sub(r"[^\w\s-]", "", t)
    t = re.sub(r"[\s_]+", "-", t)
    t = re.sub(r"-+", "-", t)
    return t.strip("-")


def lb_film_url(title, year=""):
    """Slug-based Letterboxd URL, falling back to a search URL."""
    slug = slugify(title)
    if slug:
        return f"https://letterboxd.com/film/{slug}/"
    query = urllib.parse.quote_plus(f"{title} {year}".strip())
    return f"https://letterboxd.com/search/{query}/"


# ── CLAUDE API ─────────────────────────────────────────────────────────────────

def get_recommendations(eligible_films, min_recs=None, max_recs=None):
    """Call Claude; return list of {title, year, reason} dicts."""
    if min_recs is None:
        min_recs = _cfg["recommendations"]["min"]
    if max_recs is None:
        max_recs = _cfg["recommendations"]["max"]

    if not ANTHROPIC_API_KEY:
        print("  ANTHROPIC_API_KEY not set — skipping Claude step")
        return []
    try:
        import anthropic
    except ImportError:
        print("  anthropic package not installed. Run: pip install -r requirements.txt")
        return []

    film_lines = "\n".join(
        f"- {f['title']} ({f['year']})" if f["year"] else f"- {f['title']}"
        for f in eligible_films
    )

    prompt = f"""You are recommending films to a specific viewer. Here is their taste profile:

{TASTE_PROFILE}

The following films are currently playing at {LOCATION} theatres. None appear on their watchlist or watched list — they are genuinely undiscovered options for this person. Recommend between {min_recs} and {max_recs} films from the list below that best match their taste. Prioritise films that feel personally resonant or surprising, not just critically acclaimed.

Currently playing (not watched, not watchlisted):
{film_lines}

Return ONLY a JSON array — no markdown fences, no text outside the JSON:
[
  {{"title": "Exact Title As Listed", "year": "YYYY", "reason": "One sentence why this matches their taste."}},
  ...
]

Return at least {min_recs} and at most {max_recs} items. If fewer than {min_recs} films seem like genuine fits, still return the {min_recs} closest matches."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = client.messages.create(
            model=_cfg["recommendations"]["model"],
            max_tokens=_cfg["recommendations"]["max_tokens"],
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        print(f"  Claude API error: {e}")
        return []


# ── EMAIL ──────────────────────────────────────────────────────────────────────

def _time_sort_key(t):
    m = re.match(r"(\d+):(\d+)(am|pm)", t)
    if not m:
        return (0, 0)
    h, mn, suffix = int(m.group(1)), int(m.group(2)), m.group(3)
    if suffix == "pm" and h != 12:
        h += 12
    elif suffix == "am" and h == 12:
        h = 0
    return (h, mn)


def build_reco_email(recs_by_date):
    """HTML email — recommendations grouped by day, then cinema."""
    today_str = datetime.now().strftime("%B %d, %Y")
    has_any = any(bool(v) for v in recs_by_date.values())

    html = f"""
    <html><body style="font-family:Georgia,serif;max-width:600px;margin:0 auto;
                       background:#fff;color:#1a1a1a;padding:24px;">
    <h2 style="font-size:16px;letter-spacing:0.1em;text-transform:uppercase;
               color:#666;border-bottom:1px solid #ddd;padding-bottom:8px;">
      {LOCATION} — Claude's Picks<br>
      <span style="font-size:12px;font-weight:normal;">{today_str}</span>
    </h2>
    <p style="font-size:12px;color:#888;margin-top:4px;margin-bottom:24px;">
      Films currently playing that aren't on your watchlist or watched list,
      selected by Claude based on your taste profile.
    </p>
    """

    if not has_any:
        html += "<p style='color:#666;'>No recommendations this month.</p>"
    else:
        for date_str in sorted(recs_by_date):
            films = recs_by_date[date_str]
            if not films:
                continue
            d = datetime.strptime(date_str, "%Y-%m-%d")
            day_label = d.strftime("%A, %B ") + str(d.day)
            html += f"""
            <h3 style="font-size:14px;text-transform:uppercase;letter-spacing:0.08em;
                       color:#4a7fa5;margin-top:28px;margin-bottom:10px;
                       border-bottom:1px solid #d0e4f0;padding-bottom:6px;">
              {day_label}
            </h3>
            """
            for f in films:
                venue_lines = "".join(
                    f"""<span style="display:block;font-size:12px;color:#555;margin-top:3px;">
                      <a href="{t['url']}" style="color:#4a7fa5;text-decoration:none;">{t['name']}</a>
                      &nbsp;·&nbsp; {"&ensp;".join(t['times'])}
                    </span>"""
                    for t in f["theatres"]
                )
                html += f"""
                <div style="border-left:3px solid #4a7fa5;padding:8px 12px;
                            margin-bottom:10px;background:#f5f9fd;">
                  <strong><a href="{f['url']}" style="color:#1a1a1a;text-decoration:none;">{f['title']}</a></strong>
                  <span style="color:#888;font-size:12px;margin-left:6px;">{f['year']}</span>
                  <span style="display:block;font-size:12px;color:#4a7fa5;
                               margin-top:4px;font-style:italic;">{f['reason']}</span>
                  {venue_lines}
                </div>
                """

    theatre_list = " · ".join(
        f'<a href="{url}" style="color:#aaa;">{name}</a>'
        for name, url in THEATRE_HOMEPAGES.items()
    )
    html += f"""
    <p style="font-size:11px;color:#aaa;margin-top:32px;border-top:1px solid #eee;padding-top:12px;">
      Theatres checked: {theatre_list}<br>
      Recommendations by Claude ({datetime.now().strftime("%B %Y")}).
      Showtimes via CinemaClock. Verify at venue before going.
    </p>
    </body></html>
    """
    return html


def send_email(subject, html_body):
    if not GMAIL_APP_PASSWORD:
        print("Email not configured — set GMAIL_APP_PASSWORD in .env")
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


# ── TASK SCHEDULER ─────────────────────────────────────────────────────────────

def register_scheduled_task():
    """Register/update Windows Task Scheduler entry for the recommendations email.

    Creates one schtasks task per day. Multiple days get distinct task names
    ("Letterboxd Recommendations (1)", "Letterboxd Recommendations (15)").
    Old tasks matching "Letterboxd Recommendations*" are removed first.
    """
    import subprocess
    script   = os.path.abspath(__file__)
    python   = sys.executable
    days_cfg = _cfg["recommendations"].get("schedule_days", 1)
    if isinstance(days_cfg, list):
        days = [str(d) for d in days_cfg]
    elif isinstance(days_cfg, str):
        days = [d.strip() for d in days_cfg.split(",")]
    else:
        days = [str(days_cfg)]
    time_ = _cfg["recommendations"].get("schedule_time", "21:00")

    # Remove any previously registered tasks for this script
    subprocess.run(
        ["powershell", "-Command",
         "Get-ScheduledTask | Where-Object {$_.TaskName -like 'Letterboxd Recommendations*'}"
         " | Unregister-ScheduledTask -Confirm:$false"],
        capture_output=True,
    )

    errors = []
    for d in days:
        task_name = f"Letterboxd Recommendations ({d})" if len(days) > 1 else "Letterboxd Recommendations"
        cmd = (
            f'schtasks /create /f '
            f'/tn "{task_name}" '
            f'/tr "\\"{python}\\" \\"{script}\\"" '
            f'/sc monthly /d {d} /st {time_}'
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            errors.append(f'  "{task_name}": {result.stderr.strip()}')

    day_str = ", ".join(days)
    if not errors:
        print(f'✓ Task registered — runs on day {day_str} of each month at {time_}')
    else:
        for e in errors:
            print(e)
        print("  You may need to run this from an elevated (admin) prompt.")


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"Marquee — recommendations — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")

    today = date.today()

    # 1. Fetch exclusion lists
    print("\nFetching Letterboxd exclusion lists...")
    watched_norms = fetch_watched_titles()
    print(f"  Watched (RSS):    {len(watched_norms)} titles")
    _, watchlist_entries = load_letterboxd_export()
    # Patch with recently-added watchlist items not yet in the ZIP export
    try:
        wl_xml = fetch_html(LETTERBOXD_WATCHLIST_RSS)
        zip_wl_norms = {normalize(t) for t, _, _ in watchlist_entries}
        rss_added = 0
        for item_m in re.finditer(r"<item>(.*?)</item>", wl_xml, re.DOTALL):
            title_m = re.search(
                r"<title>(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?</title>",
                item_m.group(1), re.DOTALL,
            )
            if title_m:
                raw = title_m.group(1).strip()
                year_m = re.search(r"\s*\((\d{4})\)\s*$", raw)
                title = raw[:year_m.start()].strip() if year_m else raw
                if title and normalize(title) not in zip_wl_norms:
                    watchlist_entries.append((title, year_m.group(1) if year_m else "", ""))
                    rss_added += 1
        if rss_added:
            print(f"  Watchlist RSS:    +{rss_added} recent additions")
    except Exception as e:
        print(f"  Warning: could not fetch watchlist RSS ({e})")
    watchlist_norms = {normalize(t) for t, _, _ in watchlist_entries}
    print(f"  Watchlist (total):{len(watchlist_norms)} titles")
    exclude_norms = watched_norms | watchlist_norms

    def is_excluded(title):
        norm = normalize(title)
        if norm in exclude_norms:
            return True
        return any(
            SequenceMatcher(None, norm, ex).ratio() >= EXCLUDE_THRESHOLD
            for ex in exclude_norms
        )

    # 2. Scrape theatres, merge into unique-film dict
    print("\nScraping theatres...")
    # all_films: normalized_title → {title, year, theatres: {name: {url, times_by_date}}}
    all_films: dict = {}
    for theatre_name, url in THEATRES.items():
        print(f"  {theatre_name}...", end=" ", flush=True)
        scraped = scrape_cinemaclock(url)
        print(f"{len(scraped)} films")
        homepage = THEATRE_HOMEPAGES.get(theatre_name, url)
        for film in scraped:
            norm = normalize(film["title"])
            if norm not in all_films:
                all_films[norm] = {
                    "title": film["title"],
                    "year": film["year"],
                    "theatres": {},
                }
            t_entry = all_films[norm]["theatres"].setdefault(
                theatre_name, {"url": homepage, "times_by_date": defaultdict(list)}
            )
            for st in film["showtimes"]:
                if date.fromisoformat(st["date"]) >= today:
                    t_entry["times_by_date"][st["date"]].append(st["time"])

    total_playing = len(all_films)
    print(f"\n  Total unique films playing: {total_playing}")

    # 3. Filter out watchlist + watched
    eligible = {
        norm: film for norm, film in all_films.items()
        if not is_excluded(film["title"])
    }
    excluded_count = total_playing - len(eligible)
    print(f"  Excluded (watchlist/watched): {excluded_count}")
    print(f"  Eligible for recommendation:  {len(eligible)}")

    if not eligible:
        print("No eligible films — nothing to recommend.")
        return

    # 4. Ask Claude
    print("\nAsking Claude for recommendations...")
    eligible_list = sorted(eligible.values(), key=lambda f: f["title"])
    recommendations = get_recommendations(eligible_list)
    print(f"  Received {len(recommendations)} recommendation(s)")

    if not recommendations:
        print("No recommendations returned.")
        return

    # 5. Build recs_by_date
    recs_by_date: dict = defaultdict(list)
    seen: set = set()

    for rec in recommendations:
        # Match rec title back to scraped film
        best_score, matched = 0.0, None
        for norm, film in eligible.items():
            s = similarity(rec["title"], film["title"])
            if s > best_score:
                best_score, matched = s, film
        if not matched or best_score < RECOMMENDATION_SCORE:
            print(f"  Warning: no scraped data for '{rec['title']}' (score {best_score:.2f})")
            continue

        lb_url = lb_film_url(matched["title"], matched["year"])

        # Group by date → list of theatres playing that day
        dates_theatres: dict = defaultdict(list)
        for t_name, t_info in matched["theatres"].items():
            for date_str, times in t_info["times_by_date"].items():
                dates_theatres[date_str].append({
                    "name": t_name,
                    "url": t_info["url"],
                    "times": sorted(set(times), key=_time_sort_key),
                })

        for date_str, theatres in dates_theatres.items():
            key = (matched["title"], date_str)
            if key in seen:
                continue
            seen.add(key)
            recs_by_date[date_str].append({
                "title": matched["title"],
                "year": matched["year"],
                "reason": rec.get("reason", ""),
                "url": lb_url,
                "theatres": sorted(theatres, key=lambda t: t["name"]),
            })

    for date_str in recs_by_date:
        recs_by_date[date_str].sort(key=lambda f: f["title"])

    # 6. Console summary
    print(f"\n{'─'*50}")
    print("RECOMMENDATIONS BY DAY")
    print(f"{'─'*50}")
    if not recs_by_date:
        print("No showtime data for recommended films.")
    else:
        for date_str in sorted(recs_by_date):
            d = datetime.strptime(date_str, "%Y-%m-%d")
            print(f"\n  {d.strftime('%A %b')} {d.day}")
            for f in recs_by_date[date_str]:
                print(f"    ★ {f['title']} ({f['year']})")
                print(f"      {f['reason']}")
                for t in f["theatres"]:
                    print(f"        {t['name']}: {', '.join(t['times'])}")

    # 7. Send email
    n = len(recommendations)
    subject = (
        f"🎬 {n} film pick{'s' if n != 1 else ''} for you this month — {LOCATION} theatres"
    )
    html = build_reco_email(dict(recs_by_date))
    send_email(subject, html)

    print(f"\nDone. {n} recommendation(s).\n")


if __name__ == "__main__":
    if "--schedule" in sys.argv:
        register_scheduled_task()
    else:
        main()
