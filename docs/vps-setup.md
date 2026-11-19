# Running Marquee on a Linux server (cron)

Setup for the watchlist digest on an always-on Linux box (written against Ubuntu 24.04).
`run_watchlist_checker.sh` is the cron entry point: it logs every run to
`logs/watchlist.log` and pings [healthchecks.io](https://healthchecks.io) so a failed or
missed run alerts you.

`recommendations.py` isn't wired up here yet; it would get its own wrapper and check
(monthly period) the same way.

## 1. User, code and Python

```bash
sudo adduser --disabled-password --gecos "" marquee
sudo apt install -y python3-venv git
sudo -iu marquee
git clone https://github.com/rrickyhuang/marquee.git && cd marquee
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Run as this unprivileged user, never root.

## 2. Timezone

The script uses `date.today()` for its lookahead window, so the server's clock must match
yours; a UTC server would start the window a day late for an evening run. Set it
explicitly:

```bash
sudo timedatectl set-timezone Etc/GMT+7     # UTC-7 all year, no DST
timedatectl                                  # check "Time zone"
```

(`Etc/GMT+7` is UTC-7: the sign in `Etc/` zone names is inverted by convention. For a
region that does observe DST, use its regional name, e.g. `America/Vancouver`.)

## 3. Config, data and secrets

Copy these from your computer into the project folder (they're gitignored):
`config.yaml`, `.env`, and the `letterboxd-*.zip` export.

```bash
scp -p config.yaml .env letterboxd-*.zip marquee@YOUR_VPS:marquee/    # -p keeps file times
ssh marquee@YOUR_VPS 'chmod 600 ~/marquee/.env ~/marquee/config.yaml'
```

On the VPS, edit `.env` down to what the watchlist digest needs: `GMAIL_APP_PASSWORD`
(and `HEALTHCHECKS_URL`, see below). Delete the `ANTHROPIC_API_KEY` line there; only
`recommendations.py` uses it, and it doesn't run on the server yet. Mode 600 means only
the `marquee` user can read the file.

Keep the laptop's `.env` for now: `recommendations.py` still runs from the laptop. Remove
its copy of the app password only once that script has moved to the server too (and
regenerate the app password if the laptop is ever lost).

## 4. Healthcheck

1. Create a free check at healthchecks.io. Period = your `schedule_interval_days`
   (e.g. 7 days), grace = 12 hours or so. Set up email/phone alerts.
2. Add its ping URL to `.env` (no `/start` or `/fail` suffix):
   `HEALTHCHECKS_URL=https://hc-ping.com/<uuid>`

The wrapper pings `/start`, then `/<exit status>` with the run's output attached. Exit 1
(email failed, watchlist unreadable, theatre scrape problem, ZIP fallback 14+ days old)
shows as a failure with the reason in the check's log. A run that never happens trips the
period + grace timer. Leave `HEALTHCHECKS_URL` unset to log only.

## 5. Dry run before scheduling

```bash
cd ~/marquee && ./run_watchlist_checker.sh; echo "exit: $?"
tail -n 60 logs/watchlist.log
```

This sends a real email: compare it with the one from your laptop. It's also the test for
the stable-IP question: from a datacenter IP, check that the log shows
`live` watchlist loading (not a ZIP fallback / Cloudflare failure) and that every
theatre scraped. Only look into a proxy or dedicated IP if it didn't.

## 6. Cron

`crontab -e` as the `marquee` user:

```cron
0 20 */7 * * /home/marquee/marquee/run_watchlist_checker.sh
```

Use your `watchlist.schedule_interval_days` for the `7`, and keep it at or below
`watchlist.lookahead_days`. Cron's day-of-month step restarts on the 1st, so runs are never
more than N days apart (no gaps); around month boundaries two runs can land closer
together, which just means overlapping coverage.

## 7. Log rotation

`/etc/logrotate.d/marquee` (as root):

```
/home/marquee/marquee/logs/watchlist.log {
    su marquee marquee
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
```

## 8. Cut over

After one good VPS run and one good scheduled run, disable the laptop's task so you don't
get duplicate digests (PowerShell on the laptop):

```powershell
schtasks /change /tn "WatchlistChecker" /disable
```

## Keeping the export ZIP fresh

The ZIP supplies your watched history; the live RSS only covers recent entries. Re-export
from Letterboxd every month or two and `scp -p` it over (the newest ZIP wins). Reporting
its age in the email is tracked in #35.
