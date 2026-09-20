#!/bin/bash
# Nightly refresh: collect, price, re-render, and speak only if something crossed
# a bar. Meant for cron or a systemd timer.
#
#   30 5 * * *  /path/to/congress-trades/examples/nightly-refresh.sh
#
# The digest note is always written as a local record. The LLM read and the
# notification happen only on a day an alert fired, which is the whole point:
# a job that reports every day trains you to stop reading it.
#
# Configure by environment, or drop these in an env file and point ENV_FILE at it:
#
#   CONGRESS_REPO      path to this checkout            (default: script's parent)
#   CONGRESS_NOTES     where dated notes are written    (default: ./reports)
#   CONGRESS_CONTACT   your email, required by sec.gov
#   CONGRESS_YEARS     e.g. 2026,2025,2024,2023,2022,2021
#   CONGRESS_LLM_*     endpoint/model/key for `advise`  (optional)
#   NOTIFY_CMD         command receiving the headline on stdin (optional)
#
# NOTIFY_CMD keeps this file free of anyone's home-automation details. Examples:
#
#   NOTIFY_CMD='mail -s "Congress trades" you@example.com'
#   NOTIFY_CMD='curl -sX POST -d @- https://ntfy.sh/your-topic'
#   NOTIFY_CMD='/usr/local/bin/notify-my-phone'
#
# Everything goes through run.sh rather than calling python3 directly. It picks an
# interpreter new enough for the code -- cron hands you a bare PATH, and on macOS
# that means Apple's 3.9, which cannot import this package -- and it takes the
# write lock, so a nightly firing while you have a manual refresh open waits its
# turn instead of both of them hitting "database is locked".
#
# No `set -e`: a failed LLM call or notifier must not cost you the note.
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=${CONGRESS_REPO:-$(dirname "$HERE")}
NOTES=${CONGRESS_NOTES:-$REPO/reports}
ENV_FILE=${ENV_FILE:-}
WINDOW=${ALERT_WINDOW:-14}
KEEP_DAYS=${KEEP_DAYS:-90}

# shellcheck disable=SC1090
[ -n "$ENV_FILE" ] && [ -r "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }

mkdir -p "$NOTES" || exit 1
cd "$REPO" || exit 1

CT="$REPO/run.sh"

DATE=$(date +%F)
NOTE="$NOTES/congress-$DATE.md"

# Data first. Publish is only reached if collection succeeded, so a bad night
# leaves yesterday's rendered page in place rather than replacing it with less.
if ! "$CT" all; then
  echo "collect failed; leaving the existing page and note alone" >&2
  exit 1
fi

# Read the alerts WITHOUT recording them. A recording call marks everything seen,
# so any later query -- including the one that builds the notification headline --
# comes back empty. Recording happens at the end, once they have been used.
ALERTS=$("$CT" alerts --days "$WINDOW" --dry-run)
HAVE_ALERTS=$?
HEADLINE=$("$CT" alerts --days "$WINDOW" --dry-run --headline)

{
  if [ "$HAVE_ALERTS" -eq 0 ]; then
    printf '%s\n\n---\n\n' "$ALERTS"
  else
    printf 'Quiet day: nothing crossed a bar.\n\n---\n\n'
  fi
  "$CT" digest --days 90
} > "$NOTE" || exit 1

if [ "$HAVE_ALERTS" -eq 0 ]; then
  # The LLM read costs tokens and attention, so it runs only on a day that had
  # something cross a bar.
  if [ -n "${CONGRESS_LLM_MODEL:-}" ]; then
    if ADVICE=$("$CT" advise --days 90 2>&1); then
      printf '\n---\n\n# Analysis (%s)\n\n%s\n' "$CONGRESS_LLM_MODEL" "$ADVICE" >> "$NOTE"
    else
      printf '\n---\n\nAnalysis unavailable: %s\n' "$ADVICE" >> "$NOTE"
    fi
  fi

  if [ -n "${NOTIFY_CMD:-}" ] && [ -n "$HEADLINE" ]; then
    printf '%s\n' "$HEADLINE" | eval "$NOTIFY_CMD" >/dev/null 2>&1 \
      || echo "notify failed (note still written)" >&2
  fi

  # Mark them seen only now. A crash above then leaves the backlog intact for the
  # next run instead of silently swallowing it.
  "$CT" alerts --days "$WINDOW" >/dev/null
fi

find "$NOTES" -name 'congress-*.md' -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
echo "note: $NOTE${HEADLINE:+ | headline: $HEADLINE}"
