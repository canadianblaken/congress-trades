"""Every check in the project, in one command:  ./run.sh test

Two kinds, run differently on purpose:

  tests/test_*.py      fixtures and shape checks; no network.
  module selftests     the same functions `--selftest` runs, against your real
                       data -- so they catch a change that breaks on 33,000 real
                       rows rather than on a fixture. They run on a throwaway
                       copy of the database, because a selftest that exercises a
                       writer (alerts migrates its memory, for one) must never be
                       able to touch the live file. Skipped when there is no
                       database yet, as on a fresh clone.

Exit status is the number of failures, capped at 1 for shell use.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SELFTESTS = ("advise", "alerts", "annual", "assets", "backtest", "committees",
             "compliance", "digest", "finance", "judiciary", "jurisdiction", "lag",
             "llm", "lobbying", "mcp_server", "parserqa", "resolve", "scorecard",
             "topics")
_CALL = ("import inspect, congress_trades.{m} as mod\n"
         "from congress_trades.config import CONFIG\n"
         "f = mod.selftest\n"
         "f(CONFIG) if inspect.signature(f).parameters else f()")


def _run(label: str, argv: list[str], env: dict) -> bool:
    t0 = time.monotonic()
    p = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True,
                       timeout=600)
    last = (p.stdout.strip().splitlines() or [""])[-1][:90]
    ok = p.returncode == 0
    print(f"{'ok  ' if ok else 'FAIL'} {label:<30} {time.monotonic() - t0:5.1f}s  {last}")
    if not ok:
        print("     " + (p.stderr.strip() or p.stdout.strip())[-1500:].replace("\n", "\n     "))
    return ok


def main() -> int:
    from congress_trades.config import CONFIG     # also loads .env
    env = dict(os.environ)
    results = [_run(f.name, [sys.executable, str(f)], env)
               for f in sorted((ROOT / "tests").glob("test_*.py"))]

    if not CONFIG.db_path.exists():
        print(f"skip module selftests: no database at {CONFIG.db_path}")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "congress.db"
            src = sqlite3.connect(f"file:{CONFIG.db_path}?mode=ro", uri=True)
            dst = sqlite3.connect(copy)
            src.backup(dst)
            dst.close(), src.close()
            env = {**env, "CONGRESS_DB": str(copy)}
            results += [_run(f"{m} selftest", [sys.executable, "-c", _CALL.format(m=m)], env)
                        for m in SELFTESTS]

    failed = results.count(False)
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
