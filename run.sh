#!/bin/bash
# Convenience wrapper: sets the one required variable, then hands off to the module.
#
#   ./run.sh                      refresh everything, then open the page
#   ./run.sh scorecard --limit 20 any subcommand, passed straight through
#   ./run.sh -v all               flags work too; -v goes BEFORE the subcommand
#   ./run.sh portal               live portal at http://127.0.0.1:8777
#   ./run.sh portal stop          stop a running portal (or use its Stop button)
#
# The contact address is not a key. The SEC answers 403 to anonymous automated
# clients, so `sectors` and `all` stop without it. Anything already exported wins,
# and a .env beside this file wins over the default baked in below -- keep your
# address out of git by putting it there instead.
#
# Writes are serialised with a lock. `prices` holds ONE sqlite transaction open
# across the whole pass -- 15-20 minutes on a cold cache -- so a second writing
# run dies with "database is locked" partway through collection. Waiting beats
# that: the work is resumable, a half-collected run is not. Read-only
# subcommands skip the lock, so you can query while a refresh is running.
set -uo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE" || exit 1

# shellcheck disable=SC1091
[ -r "$HERE/.env" ] && { set -a; . "$HERE/.env"; set +a; }

# No address is baked in here: it would end up in git history. Put yours in a
# .env beside this script (it is gitignored), or export it before calling.
export CONGRESS_CONTACT="${CONGRESS_CONTACT:-}"

PY=${PYTHON:-python3}

# Unbuffered, so a redirected log shows progress as it happens rather than in 8KB
# bursts -- the difference between "stalled" and "working" when you check on it.
export PYTHONUNBUFFERED=1

# Runs the module, taking the write lock unless the subcommand only reads.
# First argument is the subcommand to classify; the rest is the real command line.
run_locked() {
  local sub=$1
  shift
  case " digest scorecard backtest lag timing mix advise " in
    *" $sub "*) "$PY" -m congress_trades "$@" ; return $? ;;
  esac
  if command -v flock >/dev/null; then
    exec 9>"$HERE/.congress.lock" || exit 1
    if ! flock -n 9; then
      echo "another run holds the database; waiting for it to finish..." >&2
      flock 9 || exit 1
    fi
  fi
  "$PY" -m congress_trades "$@"
}

# The portal is its own module, not a subcommand, so this fork does not have to
# patch __main__.py and fight upstream over it on every pull.
if [ "${1:-}" = "portal" ]; then
  shift
  exec "$PY" -m congress_trades.portal "$@"
fi

# No arguments means the everyday case: bring the data current, then look at it.
if [ "$#" -eq 0 ]; then
  run_locked all -v all || exit 1
  PAGE=${CONGRESS_OUT:-$HERE/out/congress.html}
  if [ -r "$PAGE" ]; then
    echo "page: $PAGE"
    command -v xdg-open >/dev/null && xdg-open "$PAGE" >/dev/null 2>&1 &
  fi
  exit 0
fi

# The subcommand is the first argument that is not a flag.
SUB=""
for a in "$@"; do
  case "$a" in -*) ;; *) SUB=$a; break ;; esac
done

run_locked "$SUB" "$@"
