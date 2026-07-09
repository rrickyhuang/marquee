# Marquee — Project Context

Context for AI assistants working on this repo. Personal, user-specific data
(Letterboxd handle, location, taste profile, five-star films, theatres, email) lives in
`config.yaml`, which is gitignored — copy `config.example.yaml` to get started. Nothing
in this file should contain personal data; keep it that way.

## About this project
Marquee — scrapes local theatre showtimes and emails a digest of Letterboxd
watchlist matches, covering the next few days (`watchlist.lookahead_days`). A separate
`recommendations.py` script generates monthly AI-powered film recommendations.
User-specific settings (theatres, taste profile, email, location, thresholds) live in
`config.yaml`.

## Workflow note
Discuss changes in chat before implementing; Claude Code handles execution only.

## Profile & taste
The user's Letterboxd handle, location, theatre habits, taste profile, and five-star
films are all defined in `config.yaml` under `letterboxd`, `location`, `theatres`, and
`taste_profile`. `recommendations.py` passes the `taste_profile` block to Claude to
personalize recommendations.

## Theatres
Defined in `config.yaml`. Each entry has a name, CinemaClock URL (for scraping), and
homepage URL (for email links). Find CinemaClock URLs at https://www.cinemaclock.com.

## Matching Notes
- Fuzzy matching — CinemaClock titles may differ slightly from Letterboxd titles
- Thresholds live in `config.yaml` under `matching:` (confident, uncertain floor,
  exclude, recommendation score) and `watchlist.lookahead_days` for the lookahead window

## Data Sources
- Watchlist: `watchlist_checker.py` tries the dedicated RSS first, falls back to the export ZIP
- Watched (`recommendations.py` only): always loads full history from export ZIP, then layers the live master RSS on top to catch recent watches not yet exported
- Watchlist exclusion (`recommendations.py`): ZIP only — Letterboxd master RSS has no watchlist data
- Export ZIP: drop any `letterboxd-*.zip` into the project folder; scripts auto-select the most recently modified one. Gitignored.

## Email & Scheduling
- Watchlist digest: `watchlist_checker.py` — runs every `watchlist.schedule_interval_days`
  at `watchlist.schedule_time` (Windows Task Scheduler, `/sc daily /mo N`). Kept in step
  with `watchlist.lookahead_days` so runs cover the calendar without gaps or overlap.
- Monthly recommendations: `recommendations.py` — day(s)/time from `config.yaml` (`recommendations.schedule_*`), via Windows Task Scheduler
- Secrets in `.env`: `GMAIL_APP_PASSWORD`, `ANTHROPIC_API_KEY` (see `.env.example`)
- Email addresses and all other settings in `config.yaml`
- Digest grouped by: day → film → cinema → showtimes
- Film titles link to Letterboxd. Theatre names link to theatre homepage.
