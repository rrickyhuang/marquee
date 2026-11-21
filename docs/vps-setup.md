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

Run as this unprivileged user, never root. It has no password and no SSH key, so you
can't log in as it directly: get a shell with `sudo -iu marquee` from your admin account.

**Outbound mail.** Marquee sends through `smtp.gmail.com` on port 587 (STARTTLS), not
465: many VPS providers block outbound ports 25 and 465 by default, and a blocked port
makes every email fail (after minutes of hanging, on older code). 587 is normally open.
Check from the server:

```bash
timeout 8 bash -c 'echo > /dev/tcp/smtp.gmail.com/587' && echo "587 open"
```

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

The `marquee` user can't receive an `scp` itself (no SSH key), so stage the files in your
own login's home and install them with the right owner and mode:

```bash
# on your computer (-p keeps file times; the newest ZIP wins)
ssh YOUR_LOGIN@YOUR_VPS 'mkdir -p -m 700 marquee-incoming'
scp -p config.yaml .env letterboxd-*.zip YOUR_LOGIN@YOUR_VPS:marquee-incoming/

# on the VPS, as your login
cd ~/marquee-incoming
sudo install -p -o marquee -g marquee -m 600 -t /home/marquee/marquee config.yaml .env letterboxd-*.zip
rm -f config.yaml .env letterboxd-*.zip
```

`install -m 600` also fixes the mode on the ZIP, which can arrive world-writable when
copied from Windows. (Alternatively, add your public key to
`/home/marquee/.ssh/authorized_keys` and `scp` straight to `marquee@YOUR_VPS:marquee/`.)

On the VPS, trim `.env` to what the watchlist digest needs: `GMAIL_APP_PASSWORD` (and
`HEALTHCHECKS_URL`, see below). Delete the `ANTHROPIC_API_KEY` line; only
`recommendations.py` uses it, and it doesn't run on the server yet:

```bash
sudo -u marquee sed -i '/^ANTHROPIC_API_KEY=/d' /home/marquee/marquee/.env
```

A laptop `.env` may also contain `GMAIL_ADDRESS` (an optional sender override that falls
back to `email.from` in `config.yaml`; keep it if it differs) and `NOTIFY_EMAIL` (unused:
the recipient is `email.to` in `config.yaml`). Mode 600 means only the `marquee` user can
read the file.

Keep the laptop's `.env` for now: `recommendations.py` still runs from the laptop. Remove
its copy of the app password only once that script has moved to the server too (and
regenerate the app password if the laptop is ever lost).

## 4. Healthcheck

1. Create a free check at healthchecks.io. Use the Simple schedule: period = your
   `schedule_interval_days` (e.g. 7 days), grace = 12 hours or so. Set up email/phone
   alerts.
2. Add its ping URL to `.env` (no `/start` or `/fail` suffix):
   `HEALTHCHECKS_URL=https://hc-ping.com/<uuid>`. Anyone with the URL can fake a success
   ping, so enter it at a prompt rather than pasting it into a chat:

   ```bash
   read -rp "Ping URL: " U; printf '\nHEALTHCHECKS_URL=%s\n' "$U" | sudo -u marquee tee -a /home/marquee/marquee/.env >/dev/null
   ```

The wrapper pings `/start`, then `/<exit status>` with the run's output attached. Exit 1
(email failed, watchlist unreadable, theatre scrape problem, ZIP fallback 14+ days old)
shows as a failure with the reason in the check's log. A run that never happens trips the
period + grace timer. Leave `HEALTHCHECKS_URL` unset to log only.

Test both paths once without sending any email, by swapping the Python step
(`MARQUEE_PYTHON`) for `true` / `false`, as the `marquee` user in `~/marquee`:

```bash
MARQUEE_PYTHON=/bin/true  ./run_watchlist_checker.sh   # /start then /0: check goes green
MARQUEE_PYTHON=/bin/false ./run_watchlist_checker.sh   # /start then /1: check goes down, alert fires
MARQUEE_PYTHON=/bin/true  ./run_watchlist_checker.sh   # back to green
```

Each run adds a start/end line to `logs/watchlist.log`; that's expected.

## 5. Dry run before scheduling

```bash
cd ~/marquee && ./run_watchlist_checker.sh; echo "exit: $?"
tail -n 60 logs/watchlist.log
```

This sends a real email: compare it with the one from your laptop. It's also the test for
the stable-IP question: from a datacenter IP, check that the log shows
`live` watchlist loading (not a ZIP fallback / Cloudflare failure) and that every
theatre scraped. Only look into a proxy or dedicated IP if it didn't.

A run takes about 30 seconds. If it sits for minutes and then logs
`Email failed: [Errno 110] Connection timed out`, outbound SMTP is blocked (see §1).

## 6. Cron

`crontab -e` as the `marquee` user (or `echo '<line>' | sudo -u marquee crontab -` from your
admin account):

```cron
0 20 */7 * * /home/marquee/marquee/run_watchlist_checker.sh
```

Restart cron once after the timezone change in §2 (`sudo systemctl restart cron`) so it
is sure to use the new zone. With `*/7` the runs fall on days 1, 8, 15, 22 and 29.

Use your `watchlist.schedule_interval_days` for the `7`, and keep it at or below
`watchlist.lookahead_days`. Cron's day-of-month step restarts on the 1st, so runs are never
more than N days apart (no gaps); around month boundaries two runs can land closer
together, which just means overlapping coverage.

## 7. Log rotation

`/etc/logrotate.d/marquee` (owned by root, mode 644; logrotate ignores configs others can
write):

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

Validate it with a dry run. It must run as root, because the `su` line switches user:

```bash
sudo logrotate -d /etc/logrotate.d/marquee
```

Look for errors such as "insecure permissions"; "does not need rotating" is normal on a
dry run. Rotation itself runs from the system's daily `logrotate.timer`.

## 8. Cut over

After one good VPS run and one good scheduled run, disable the laptop's task so you don't
get duplicate digests (PowerShell on the laptop):

```powershell
schtasks /change /tn "WatchlistChecker" /disable
```

## Keeping the export ZIP fresh

The ZIP supplies your watched history; the live RSS only covers recent entries. Re-export
from Letterboxd every month or two and copy it over as in §3 (the newest ZIP wins).
Reporting its age in the email is tracked in #35.
