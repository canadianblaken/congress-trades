#!/bin/bash
# Convenience wrapper: sets the one required variable, then hands off to the module.
#
#   ./run.sh                      refresh everything, then open the page
#   ./run.sh scorecard --limit 20 any subcommand, passed straight through
#   ./run.sh -v all               flags work too; -v goes BEFORE the subcommand
#   ./run.sh portal               live portal at http://127.0.0.1:8777
#   ./run.sh portal stop          stop a running portal (or use its Stop button)
#
# Linux and macOS both. Nothing below needs bash 4 -- macOS still ships 3.2 as
# /bin/bash -- and the two tools that exist only on Linux, flock and xdg-open,
# have fallbacks further down rather than being skipped silently.
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

# The code is 3.10+ (`X | None` annotations are evaluated at import), and an older
# interpreter fails with a TypeError from deep inside a module rather than
# anything you could act on. Apple ships 3.9 as python3, so a Mac hits this
# first. Check once; if python3 is too old, use a newer one sitting beside it.
py_ok() {
  command -v "$1" >/dev/null 2>&1 &&
    "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null
}

PY=${PYTHON:-python3}
if ! py_ok "$PY"; then
  # An explicit PYTHON is a decision, not a default: report it instead of
  # quietly running something else.
  if [ -z "${PYTHON:-}" ]; then
    for cand in python3.14 python3.13 python3.12 python3.11 python3.10; do
      if py_ok "$cand"; then
        echo "python3 is older than 3.10; using $cand (set PYTHON= to override)" >&2
        PY=$cand
        break
      fi
    done
  fi
fi
if ! py_ok "$PY"; then
  echo "need python 3.10 or newer; $PY is older than that or not installed." >&2
  echo "  macOS:          brew install python@3.12" >&2
  echo "  Debian/Ubuntu:  sudo apt install python3" >&2
  echo "  or choose one:  PYTHON=/path/to/python3.12 ./run.sh $*" >&2
  exit 1
fi

# Unbuffered, so a redirected log shows progress as it happens rather than in 8KB
# bursts -- the difference between "stalled" and "working" when you check on it.
export PYTHONUNBUFFERED=1

LOCK="$HERE/.congress.lock"

# Takes the write lock, waiting if another run holds it.
#
# flock is Linux-only. Where it is missing (macOS) we fall back to mkdir, which
# is atomic everywhere, and record the pid inside: a lock left behind by a
# process that was killed is then reclaimed by the next run instead of wedging
# every future one. The flock path keeps its kernel-released lock on Linux.
take_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK" || exit 1
    if ! flock -n 9; then
      echo "another run holds the database; waiting for it to finish..." >&2
      flock 9 || exit 1
    fi
    return 0
  fi
  local said="" held=""
  while ! mkdir "$LOCK.d" 2>/dev/null; do
    held=$(cat "$LOCK.d/pid" 2>/dev/null)
    # An empty pid means the holder has the directory but has not written it
    # yet, which is a live run mid-handshake -- wait, do not reclaim.
    if [ -n "$held" ] && ! kill -0 "$held" 2>/dev/null; then
      echo "lock left behind by dead process $held; reclaiming it" >&2
      rm -rf "$LOCK.d"
      continue
    fi
    [ -z "$said" ] && echo "another run holds the database; waiting for it to finish..." >&2
    said=1
    sleep 2
  done
  echo $$ >"$LOCK.d/pid"
  # Released on any ordinary exit, Ctrl-C included. SIGKILL leaves it behind,
  # which is exactly what the pid check above is for.
  trap 'rm -rf "$LOCK.d"' EXIT INT TERM
}

# Runs the module, taking the write lock unless the subcommand only reads.
# First argument is the subcommand to classify; the rest is the real command line.
run_locked() {
  local sub=$1
  shift
  case " digest scorecard backtest lag timing mix advise " in
    *" $sub "*) "$PY" -m congress_trades "$@" ; return $? ;;
  esac
  take_lock
  "$PY" -m congress_trades "$@"
}

# Hands the rendered page to the desktop. xdg-open is tried first on purpose:
# some Linux installs have an /usr/bin/open that is openvt, which would switch
# the virtual console instead of showing anything.
open_page() {
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1" >/dev/null 2>&1 &
  elif [ "$(uname -s)" = "Darwin" ]; then
    open "$1" >/dev/null 2>&1 &
  fi
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
    open_page "$PAGE"
  fi
  exit 0
fi

# The subcommand is the first argument that is not a flag.
SUB=""
for a in "$@"; do
  case "$a" in -*) ;; *) SUB=$a; break ;; esac
done

run_locked "$SUB" "$@"
