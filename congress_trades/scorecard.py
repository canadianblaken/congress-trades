"""Per-member performance: how each filer's disclosed trades actually turned out.

Scored against SPY over the identical window, because raw returns rank members by
when they happened to file rather than by anything they did. In a market that
returned +7% over 90 days, a member whose buys returned +6% was behind, and one
whose sells were followed by a +4% rise still avoided 3 points of the index.

Both directions count, symmetrically:

    buy   alpha = stock − benchmark      they chose to hold it
    sell  alpha = benchmark − stock      they chose not to; the index was the
                                         alternative, so a name that then lagged
                                         the market is a sell that paid

Every number here is measured from the DISCLOSURE date. Trade-date returns would
flatter these members substantially and mean nothing, since nobody outside the
filing knew until the filing.

Sample sizes are small and the windows overlap, so a median across trades is not
a portfolio return and MIN_TRADES is enforced rather than suggested.
"""
from __future__ import annotations

import statistics

from . import db
from .config import CONFIG

MIN_TRADES = 10          # below this a "record" is one lucky quarter
HORIZONS = ("30", "90")


def alpha(tx_type: str, ret: float | None, bench: float | None) -> float | None:
    """Excess return in the direction the member took. None if either leg is
    missing -- an unmeasurable trade must not score as a zero."""
    if ret is None or bench is None:
        return None
    return (bench - ret) if tx_type == "sell" else (ret - bench)


def load(floor: int, cfg=CONFIG):
    with db.connect(cfg.db_path) as conn:
        return [dict(r) for r in conn.execute(
            """SELECT t.member, t.chamber, t.ticker, t.tx_type, t.amount_min,
                      t.amount_range, t.owner, t.asset_name,
                      COALESCE(NULLIF(t.disclosed,''), t.tx_date) AS d0,
                      r.ret_30, r.ret_90, r.ret_now,
                      r.bench_30, r.bench_90, r.bench_now
                 FROM congress_trades t
                 JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker != '' AND t.amount_min >= ?
                ORDER BY d0 DESC""", (floor,))]


def _summary(trades, horizon="90"):
    """Median alpha and beat rate over trades that have a closed window."""
    a = [x for x in (alpha(t["tx_type"], t[f"ret_{horizon}"], t[f"bench_{horizon}"])
                     for t in trades) if x is not None]
    if not a:
        return {"n": 0, "med": None, "beat": None}
    return {"n": len(a), "med": statistics.median(a),
            "beat": sum(1 for x in a if x > 0) / len(a)}


def _notable(trades, horizon="90"):
    """The single best and worst call, kept whole so the caller can say what
    actually happened rather than only how much."""
    scored = [(alpha(t["tx_type"], t[f"ret_{horizon}"], t[f"bench_{horizon}"]), t)
              for t in trades]
    scored = [(a, t) for a, t in scored if a is not None]
    if not scored:
        return None, None
    scored.sort(key=lambda p: p[0])
    return scored[-1], scored[0]


def members(floor: int = 15001, horizon: str = "90", min_trades: int = MIN_TRADES,
            cfg=CONFIG) -> list[dict]:
    rows = load(floor, cfg)
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["member"], []).append(r)

    out = []
    for name, trades in by.items():
        buys = [t for t in trades if t["tx_type"] == "buy"]
        sells = [t for t in trades if t["tx_type"] == "sell"]
        overall = _summary(trades, horizon)
        if overall["n"] < min_trades:
            continue
        best, worst = _notable(trades, horizon)
        # Trades whose window has not closed: what they look like right now.
        open_ = [t for t in trades if t[f"ret_{horizon}"] is None
                 and t["ret_now"] is not None]
        out.append({
            "member": name,
            "chamber": trades[0]["chamber"],
            "trades": len(trades),
            "overall": overall,
            "buys": _summary(buys, horizon),
            "sells": _summary(sells, horizon),
            "open": _summary(open_, "now"),
            "open_n": len(open_),
            "best": {"alpha": best[0], **best[1]} if best else None,
            "worst": {"alpha": worst[0], **worst[1]} if worst else None,
            "last": trades[0]["d0"],
        })
    out.sort(key=lambda o: -o["overall"]["med"])
    return out


def describe(t: dict, a: float) -> str:
    """One plain sentence about a single call, with the market's move in it --
    'sold before it dropped 30%' is mostly beta if the market dropped 25%."""
    ret, bench = t.get("ret_90"), t.get("bench_90")
    if ret is None or bench is None:
        return ""
    if t["tx_type"] == "sell":
        moved = (f"then fell {abs(ret)*100:.0f}%" if ret < 0
                 else f"then rose {ret*100:.0f}%")
        return (f"sold {t['ticker']} on {t['d0']}; it {moved} over 90 days while the "
                f"market did {bench*100:+.0f}% ({a*100:+.0f}% vs holding the index)")
    moved = f"rose {ret*100:.0f}%" if ret > 0 else f"fell {abs(ret)*100:.0f}%"
    return (f"bought {t['ticker']} on {t['d0']}; it {moved} over 90 days while the "
            f"market did {bench*100:+.0f}% ({a*100:+.0f}% vs the index)")


def pct(x, digits=1):
    return "—" if x is None else f"{x*100:+.{digits}f}%"


def rate(x):
    return "—" if x is None else f"{x*100:.0f}%"


def to_markdown(rows: list[dict], horizon="90", limit=0) -> str:
    shown = rows[:limit] if limit else rows
    L = [f"# Member scorecard — {horizon}-day alpha vs SPY, from the disclosure date",
         "",
         f"{len(rows)} members with at least {MIN_TRADES} measurable trades. "
         "Alpha is excess return in the direction the member took: a buy scores "
         "stock − index, a sell scores index − stock, so exiting a name that then "
         "lagged the market counts as a win.",
         "",
         "| # | member | trades | median alpha | beat index | buys | sells | open now |",
         "|--:|---|--:|--:|--:|--:|--:|--:|"]
    for i, m in enumerate(shown, 1):
        L.append(f"| {i} | {m['member']} ({m['chamber'][:1]}) | {m['overall']['n']} "
                 f"| {pct(m['overall']['med'])} | {rate(m['overall']['beat'])} "
                 f"| {pct(m['buys']['med'])} ({m['buys']['n']}) "
                 f"| {pct(m['sells']['med'])} ({m['sells']['n']}) "
                 f"| {pct(m['open']['med'])} ({m['open_n']}) |")

    L += ["", "## Best and worst single calls"]
    for m in shown:
        if not m["best"]:
            continue
        L.append(f"- **{m['member']}** — best: {describe(m['best'], m['best']['alpha'])}")
        if m["worst"] and m["worst"] is not m["best"]:
            L.append(f"  - worst: {describe(m['worst'], m['worst']['alpha'])}")
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    assert alpha("buy", 0.10, 0.07) == 0.030000000000000002 or \
        abs(alpha("buy", 0.10, 0.07) - 0.03) < 1e-9
    assert abs(alpha("sell", -0.10, 0.05) - 0.15) < 1e-9, "sell alpha sign"
    assert alpha("sell", None, 0.05) is None and alpha("buy", 0.1, None) is None
    rows = members(cfg=cfg)
    assert rows, "no members scored -- run `congress-trades prices` first"
    meds = [m["overall"]["med"] for m in rows]
    assert meds == sorted(meds, reverse=True), "not ranked by median alpha"
    assert all(m["overall"]["n"] >= MIN_TRADES for m in rows), "min sample not enforced"
    for m in rows:
        assert m["buys"]["n"] + m["sells"]["n"] <= m["trades"]
        if m["best"] and m["worst"]:
            assert m["best"]["alpha"] >= m["worst"]["alpha"]
    txt = to_markdown(rows)
    assert "median alpha" in txt and "Best and worst" in txt
    print(f"selftest ok: {len(rows)} members scored, "
          f"best {rows[0]['member']} {pct(rows[0]['overall']['med'])}, "
          f"worst {rows[-1]['member']} {pct(rows[-1]['overall']['med'])}")
