# 🎬 Marquee

**Your Letterboxd watchlist, matched against what's actually screening near you — delivered to your inbox.**

Marquee scrapes local theatre showtimes, matches them against your Letterboxd
watchlist, and emails you a weekly digest of the films you want to see that are
playing this week. A companion script asks Claude for monthly recommendations from
what's currently screening, based on your taste profile.

It's built for repertory- and arthouse-heavy cities: if your theatres are listed on
[CinemaClock](https://www.cinemaclock.com), Marquee can watch them for you.

---

## What you get

- **Weekly watchlist digest** (`watchlist_checker.py`) — an email grouped by day → film
  → cinema → showtimes, covering the next several days. Confident matches are
  highlighted; uncertain fuzzy matches are flagged separately to verify. Films you've
  already watched are tagged as rewatches.
- **Monthly recommendations** (`recommendations.py`) — excludes everything you've
  already watched or watchlisted, then asks Claude to pick films currently playing that
  fit your taste, with a one-line reason for each.

Both send styled HTML emails via Gmail and can register themselves as scheduled tasks.

## Requirements

- Python 3.9+
- A Gmail account (for sending the digest) with an [App Password](https://support.google.com/accounts/answer/185833)
- An [Anthropic API key](https://console.anthropic.com/) — only for `recommendations.py`
- Theatres that are listed on [CinemaClock](https://www.cinemaclock.com)

## Setup

1. **Install dependencies:**
   ```
   pip install -r requirements.txt
   ```
2. **Create your config:** copy `config.example.yaml` → `config.yaml` and fill in your
   Letterboxd username, theatres, email, location, taste profile, and matching
   thresholds. Every field is documented inline in the example.
   - Find each theatre's CinemaClock URL by searching at
     [cinemaclock.com](https://www.cinemaclock.com) and copying the theatre page URL.
3. **Add your secrets:** copy `.env.example` → `.env` and fill in:
   - `GMAIL_APP_PASSWORD` — Google Account → Security → 2-Step Verification → App
     passwords → generate one for "Mail"
   - `ANTHROPIC_API_KEY` — required for `recommendations.py` only
4. **Add your Letterboxd data:** export your data from Letterboxd (Settings → Data →
   Export your data) and drop the `letterboxd-*.zip` into the project folder. Both
   scripts auto-select the most recently modified export. Refresh it periodically to
   stay current.

`config.yaml`, `.env`, and the export ZIP are all gitignored — your personal data never
gets committed.

## Usage

Run either script manually (works on any OS):
```
python watchlist_checker.py     # weekly watchlist digest
python recommendations.py       # monthly AI recommendations
```

Run the tests:
```
pytest
```

### Scheduling (Windows)

On Windows, each script can register itself with Task Scheduler using the day/time from
`config.yaml`:
```
python watchlist_checker.py --schedule      # weekly
python recommendations.py --schedule        # monthly
```

On macOS/Linux, `--schedule` isn't supported yet — use `cron` to run the scripts on your
own schedule (e.g. `0 20 * * 0 python /path/to/watchlist_checker.py` for Sundays at 8pm).

## How it works

- **Watchlist source:** `watchlist_checker.py` tries the dedicated Letterboxd watchlist
  RSS feed first, falling back to the export ZIP. (Letterboxd's watchlist RSS is often
  Cloudflare-blocked, so in practice the ZIP is the primary source — keep it fresh.)
- **Watched history** (`recommendations.py`): loads full history from the export ZIP,
  then layers the live activity RSS on top to catch recent watches not yet exported.
- **Matching:** theatre listings are fuzzy-matched against Letterboxd titles, since
  CinemaClock titles can differ slightly. Thresholds are configurable in `config.yaml`
  under `matching:`.

## Limitations

- **CinemaClock-only.** Marquee scrapes CinemaClock, so it only works for theatres
  listed there. Scraping depends on CinemaClock's current HTML — if they redesign, the
  parser may need updating.
- **Windows-first scheduling.** Manual runs are cross-platform; automatic scheduling is
  Windows-only for now (see above for the cron workaround).
- **Gmail-based email.** Sending uses Gmail SMTP with an app password. Other providers
  would need a small change to `send_email()`.

## Project layout

| File | Purpose |
|---|---|
| `watchlist_checker.py` | Weekly watchlist-match digest |
| `recommendations.py` | Monthly AI recommendation digest |
| `config.example.yaml` | Template for your `config.yaml` (gitignored) |
| `.env.example` | Template for your secrets (`.env`, gitignored) |
| `test_schedule.py` | Tests for the scheduling logic |
| `conftest.py` | Pytest setup (stubs config so scripts import cleanly) |
| `CLAUDE.md` | Context for AI assistants working on the repo |

## Contributing

Issues and pull requests are welcome. Known improvements and good first tasks are
tracked in the [issue tracker](https://github.com/rrickyhuang/letterboxd-watchlist/issues)
— several are self-contained (unused imports, docstring fixes, added test coverage).

## License

[MIT](LICENSE) © 2026 Ricky Huang

Not affiliated with Letterboxd or CinemaClock. Showtimes are scraped from CinemaClock;
always verify at the venue before heading out.
