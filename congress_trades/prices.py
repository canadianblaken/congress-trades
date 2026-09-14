"""Daily closes per ticker, reduced to one forward-return row per disclosure.

Why this exists: the disclosures alone say what members traded, never whether it
worked out. Without prices any "should I follow this" question is unanswerable,
so the digest would be ranking convergence and calling it a signal.

Why only derived returns land in the database: ten years of daily closes for
~1,300 tickers is millions of rows to store and re-store for the handful of dates
we actually care about. The raw series is cached on disk (cache/prices/, already
gitignored) and thrown away; the database keeps one row per trade.

Source is Yahoo's public chart endpoint -- no key, no account. Stooq was the
first choice and now sits behind a JavaScript proof-of-work wall, which is not
worth defeating for daily closes.
"""
from __future__ import annotations

import datetime as dt
import json
import time

import requests

from . import db
from .config import CONFIG, user_agent

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
CACHE_TTL = 20 * 3600          # a trading day; closes never change retroactively
PAUSE = 0.25                   # Yahoo starts 429ing a few requests per second


def _cache_path(cfg, ticker: str):
    d = cfg.cache_dir / "prices"
    d.mkdir(parents=True, exist_ok=True)
    # BRK.B and friends: keep the filename flat rather than a nested directory.
    return d / f"{ticker.replace('/', '_').replace('.', '-')}.json"


def series(ticker: str, cfg=CONFIG, force: bool = False) -> dict[str, float]:
    """date -> close for one ticker, cached. {} when Yahoo has no such symbol."""
    path = _cache_path(cfg, ticker)
    if not force and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        return json.loads(path.read_text())

    try:
        r = requests.get(CHART.format(sym=ticker), timeout=30,
                         params={"range": "10y", "interval": "1d"},
                         headers={"User-Agent": user_agent(cfg)})
        if r.status_code == 404:
            path.write_text("{}")          # cache the miss; many filings name funds
            return {}
        r.raise_for_status()
        res = (r.json().get("chart") or {}).get("result") or []
        if not res:
            path.write_text("{}")
            return {}
        node = res[0]
        stamps = node.get("timestamp") or []
        closes = (node.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        out = {dt.date.fromtimestamp(t).isoformat(): c
               for t, c in zip(stamps, closes) if c is not None}
    except (requests.RequestException, ValueError, KeyError):
        return {}                          # transient: no cache write, retry next run

    path.write_text(json.dumps(out, separators=(",", ":")))
    return out


def _close_at_or_after(s: dict[str, float], day: str, window: int = 7):
    """Closes only exist on trading days, so a disclosure landing on a Saturday or
    a holiday has to walk forward to the next session."""
    d = dt.date.fromisoformat(day)
    for i in range(window):
        v = s.get((d + dt.timedelta(days=i)).isoformat())
        if v:
            return v
    return None


def compute(cfg=CONFIG, limit: int = 0, quiet: bool = False) -> int:
    """Fill trade_returns for every tickered disclosure. Re-runnable: rows whose
    90-day window has closed are final and are skipped on later runs."""
    today = dt.date.today()
    with db.connect(cfg.db_path) as conn:
        rows = conn.execute(
            """SELECT t.id, t.ticker, COALESCE(NULLIF(t.disclosed,''), t.tx_date) AS d0
                 FROM congress_trades t
                 LEFT JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker != '' AND d0 != ''
                  AND (r.trade_id IS NULL OR r.ret_90 IS NULL)
                ORDER BY d0 DESC""").fetchall()
        if limit:
            rows = rows[:limit]

        by_ticker: dict[str, list] = {}
        for r in rows:
            by_ticker.setdefault(r["ticker"], []).append(r)

        done = miss = 0
        for i, (ticker, trades) in enumerate(sorted(by_ticker.items())):
            s = series(ticker, cfg)
            time.sleep(PAUSE)
            if not s:
                miss += 1
                continue
            last = max(s)
            px_now = s[last]
            for t in trades:
                d0 = dt.date.fromisoformat(t["d0"])
                p0 = _close_at_or_after(s, t["d0"])
                if not p0:
                    continue
                p30 = _close_at_or_after(s, (d0 + dt.timedelta(days=30)).isoformat())
                p90 = _close_at_or_after(s, (d0 + dt.timedelta(days=90)).isoformat())
                # A window that has not elapsed yet stays NULL so the next run fills it.
                if p30 and d0 + dt.timedelta(days=30) > today:
                    p30 = None
                if p90 and d0 + dt.timedelta(days=90) > today:
                    p90 = None
                conn.execute(
                    """INSERT OR REPLACE INTO trade_returns
                       (trade_id, px_0, px_30, px_90, px_now,
                        ret_30, ret_90, ret_now, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (t["id"], p0, p30, p90, px_now,
                     (p30 / p0 - 1) if p30 else None,
                     (p90 / p0 - 1) if p90 else None,
                     (px_now / p0 - 1),
                     db.utcnow()))
                done += 1
            if not quiet and i % 25 == 0:
                print(f"  {i}/{len(by_ticker)} tickers")
        print(f"priced {done} disclosures, {miss} tickers with no series")
    return 0
