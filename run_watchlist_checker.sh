#!/usr/bin/env bash
# Cron wrapper for the watchlist digest (Linux counterpart of run_watchlist_checker.bat).
#
# - appends everything the script prints (stdout + stderr) to logs/watchlist.log
# - pings healthchecks.io (if HEALTHCHECKS_URL is set) at the start and end of the
#   run; the end ping carries the exit status and the tail of this run's output, so
#   a failed run alerts immediately and says why
# - exits with the script's own exit status
#
# HEALTHCHECKS_URL comes from the environment or .env (the ping URL, without a
# trailing /start or /fail). If it's unset the run is only logged.
# MARQUEE_PYTHON overrides the interpreter (default: .venv/bin/python, else python3).

set -u
cd "$(dirname "$(readlink -f "$0")")" || exit 1

PY="${MARQUEE_PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
fi

HC_URL="${HEALTHCHECKS_URL:-}"
if [ -z "$HC_URL" ] && [ -f .env ]; then
    # Parsed with grep rather than sourced: values like the Gmail app password contain spaces.
    HC_URL=$(grep -E '^HEALTHCHECKS_URL=' .env | tail -n 1 | cut -d= -f2- | tr -d '\r "')
fi
HC_URL="${HC_URL%/}"

mkdir -p logs
LOG=logs/watchlist.log
OUT=$(mktemp)
trap 'rm -f "$OUT"' EXIT

stamp() { date '+%Y-%m-%d %H:%M:%S %z'; }

ping_hc() {  # ping_hc <path suffix> [body on stdin]
    [ -n "$HC_URL" ] || return 0
    if ! curl -fsS -m 10 --retry 3 -o /dev/null --data-binary @- "$HC_URL$1" 2>>"$LOG"; then
        echo "  ! healthcheck ping to $1 failed" >> "$LOG"
    fi
}

echo "=== $(stamp) run start ===" >> "$LOG"
ping_hc /start < /dev/null

"$PY" watchlist_checker.py > "$OUT" 2>&1
rc=$?

cat "$OUT" >> "$LOG"
echo "=== $(stamp) run end, exit $rc ===" >> "$LOG"

# Healthchecks treats /<n> as success for 0 and failure for 1-255, and stores the body.
tail -c 10000 "$OUT" | ping_hc "/$rc"

exit "$rc"
