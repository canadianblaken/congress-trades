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
BENCHMARK = "SPY"             # what the money would have done sitting in the market

# A second benchmark, per sector, to separate stock picking from sector timing.
# A member who only bought semiconductors through a semiconductor rally beats SPY
# without having picked anything; measured against SOXX the same trades read flat.
#
# The mapping is a judgement call, not a definition. These labels are rolled up
# from SEC SIC codes and some buckets genuinely straddle two ETFs -- "Pharma &
# Chemicals" is mostly healthcare with industrial chemicals mixed in, and
# "Machinery & Computer Equipment" is where SIC files Apple. Each label goes to
# the ETF that fits the bulk of its volume; anything unmapped falls back to the
# broad market rather than silently scoring against nothing.
#
# Known bad case: ETFs and trusts inherit the SIC of their sponsor, so a spot
# bitcoin fund files under "Commodity Contracts Brokers & Dealers" and rolls up
# to Banking & Finance -- which benchmarks bitcoin against banks. Sector alpha on
# any fund holding is noise for this reason; read it only for operating companies.
SECTOR_ETF = {
    "Software & IT Services": "XLK",
    "Semiconductors": "SOXX",
    "Machinery & Computer Equipment": "XLK",
    "Electronics & Electrical Equipment": "XLK",
    "Communications Equipment": "XLK",
    "Communications": "XLC",
    "Pharma & Chemicals": "XLV",
    "Instruments & Medical Devices": "XLV",
    "Healthcare Services": "XLV",
    "Banking & Finance": "XLF",
    "Insurance": "XLF",
    "Retail": "XLY",
    "Consumer Services": "XLY",
    "Textiles & Apparel": "XLY",
    "Food & Beverage": "XLP",
    "Wholesale": "XLP",
    "Agriculture": "XLP",
    "Utilities & Power": "XLU",
    "Mining & Energy Extraction": "XLE",
    "Petroleum Refining": "XLE",
    "Materials": "XLB",
    "Metals & Fabrication": "XLB",
    "Paper, Wood & Publishing": "XLB",
    "Transportation Equipment": "XLI",
    "Transportation & Logistics": "XLI",
    "Construction & Engineering": "XLI",
    "Professional Services": "XLI",
    "HVAC & Refrigeration": "XLI",
    "Misc Manufacturing": "XLI",
    "Real Estate (REIT)": "XLRE",
    "Real Estate": "XLRE",
}
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


def cached(ticker: str, cfg=CONFIG) -> dict[str, float]:
    """The cached closes for one ticker, or {} -- never a network call.

    `series` refreshes anything older than a trading day, which is right for a
    pipeline pass and wrong for a page load: the portal draws whatever the last
    refresh left behind rather than making a reader wait on Yahoo."""
    path = _cache_path(cfg, ticker)
    if not path.exists():
        return {}
    try:
        out = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


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
    90-day window has closed are final and are skipped on later runs.

    Every row also stores the benchmark over the identical window, because a
    return without one is unreadable: +6% on a 90-day window when the market did
    +7% is a loss, and every member looks like a genius in a bull market."""
    today = dt.date.today()
    bench = series(BENCHMARK, cfg)
    if not bench:
        print(f"warning: no {BENCHMARK} series; returns will have no benchmark")
    # One series per sector ETF, fetched once and shared by every trade in it.
    etfs = {e: series(e, cfg) for e in sorted(set(SECTOR_ETF.values()))}
    etfs = {e: v for e, v in etfs.items() if v}
    with db.connect(cfg.db_path) as conn:
        rows = conn.execute(
            """SELECT t.id, t.ticker, COALESCE(NULLIF(t.disclosed,''), t.tx_date) AS d0,
                      COALESCE(s.sector, '') AS sector
                 FROM congress_trades t
                 LEFT JOIN trade_returns r ON r.trade_id = t.id
                 LEFT JOIN ticker_sectors s ON s.ticker = t.ticker
                WHERE t.ticker != '' AND d0 != ''
                  AND (r.trade_id IS NULL OR r.ret_90 IS NULL
                       OR (r.bench_90 IS NULL AND r.ret_90 IS NOT NULL)
                       OR (r.sec_90 IS NULL AND r.ret_90 IS NOT NULL))
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
                # The benchmark is read at the same dates, not the same offsets,
                # so a holiday or a weekend shifts both legs together.
                b0 = _close_at_or_after(bench, t["d0"]) if bench else None
                b30 = b90 = b_now = None
                if b0:
                    if p30:
                        bx = _close_at_or_after(
                            bench, (d0 + dt.timedelta(days=30)).isoformat())
                        b30 = (bx / b0 - 1) if bx else None
                    if p90:
                        bx = _close_at_or_after(
                            bench, (d0 + dt.timedelta(days=90)).isoformat())
                        b90 = (bx / b0 - 1) if bx else None
                    b_now = bench[max(bench)] / b0 - 1
                # Same arithmetic against the sector ETF. Unmapped sectors get the
                # broad market, so the column always means "the obvious alternative".
                etf = SECTOR_ETF.get(t["sector"] or "")
                sec = etfs.get(etf) if etf else bench
                s30 = s90 = s_now = None
                if sec:
                    c0 = _close_at_or_after(sec, t["d0"])
                    if c0:
                        if p30:
                            cx = _close_at_or_after(
                                sec, (d0 + dt.timedelta(days=30)).isoformat())
                            s30 = (cx / c0 - 1) if cx else None
                        if p90:
                            cx = _close_at_or_after(
                                sec, (d0 + dt.timedelta(days=90)).isoformat())
                            s90 = (cx / c0 - 1) if cx else None
                        s_now = sec[max(sec)] / c0 - 1
                conn.execute(
                    """INSERT OR REPLACE INTO trade_returns
                       (trade_id, px_0, px_30, px_90, px_now,
                        ret_30, ret_90, ret_now, bench_30, bench_90, bench_now,
                        sec_30, sec_90, sec_now, sec_etf, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t["id"], p0, p30, p90, px_now,
                     (p30 / p0 - 1) if p30 else None,
                     (p90 / p0 - 1) if p90 else None,
                     (px_now / p0 - 1),
                     b30, b90, b_now,
                     s30, s90, s_now, etf or BENCHMARK,
                     db.utcnow()))
                done += 1
            if not quiet and i % 25 == 0:
                print(f"  {i}/{len(by_ticker)} tickers")
        print(f"priced {done} disclosures, {miss} tickers with no series")
    return 0
