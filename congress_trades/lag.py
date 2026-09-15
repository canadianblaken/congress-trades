"""Does a member file their best trades late?

The STOCK Act gives 45 days. A member who files a winning trade near or past that
deadline discloses it when it is least useful to anyone reading, and 3,350 of the
priced disclosures here are past the statutory window. Whether that pattern
correlates with the trade being good is testable, so test it rather than assume.

Two separate questions, because they can disagree:

  population  do late-filed trades carry more alpha than promptly filed ones?
  per member  within one member's own history, are their better trades the later
              ones? This controls for member-level confounds -- a member who is
              both a good picker and a chronically late filer would create a
              population correlation without any trade-level relationship.

Read the late-filing result carefully: every return here is measured from the
DISCLOSURE date, so a trade filed a year after execution is scored on the stock a
year after the member acted. Negative alpha on those rows is closer to "there was
nothing left to measure" than to "they traded badly", and the honest reading of
the gradient is about which disclosures are still actionable, not about which
members pick well.

ponytail: rank correlation per member, no partial-correlation model. With 109
members and overlapping windows a regression would imply more precision than
this data has.
"""
from __future__ import annotations

import datetime as dt
import statistics

from . import backtest, db, scorecard
from .config import CONFIG

STATUTORY_DAYS = 45
# A handful of filings carry an unparseable transaction date -- a year centuries
# off, giving a negative or absurd lag. They are dropped rather than bucketed,
# and counted so the drop is visible instead of silent.
MAX_SANE_LAG = 1095


def load(floor: int = 1, cfg=CONFIG):
    """floor defaults to 1, not the display floor: the $15k floor exists to keep
    rebalancing noise off the page, but it costs four fifths of the sample here
    and the lag relationship is what is being measured, not the trade size."""
    with db.connect(cfg.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT t.member, t.ticker, t.tx_type, t.tx_date, t.disclosed,
                      t.amount_min, r.ret_90, r.bench_90, r.sec_90,
                      COALESCE(NULLIF(t.disclosed,''), t.tx_date) AS d0
                 FROM congress_trades t
                 JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker != '' AND t.amount_min >= ?
                  AND t.tx_date != '' AND t.disclosed != ''
                  AND r.ret_90 IS NOT NULL AND r.bench_90 IS NOT NULL""", (floor,))]
    out, dropped = [], 0
    for r in rows:
        try:
            lag = (dt.date.fromisoformat(r["disclosed"])
                   - dt.date.fromisoformat(r["tx_date"])).days
        except ValueError:
            dropped += 1
            continue
        if lag < 0 or lag > MAX_SANE_LAG:
            dropped += 1
            continue
        r["lag"] = lag
        r["alpha"] = scorecard.alpha(r["tx_type"], r["ret_90"], r["bench_90"])
        r["salpha"] = scorecard.alpha(r["tx_type"], r["ret_90"], r["sec_90"])
        if r["alpha"] is not None:
            out.append(r)
    return out, dropped


BUCKETS = ((0, 15), (16, 30), (31, 45), (46, 90), (91, 365), (366, MAX_SANE_LAG))


def by_bucket(rows) -> list[dict]:
    out = []
    for lo, hi in BUCKETS:
        sel = [r for r in rows if lo <= r["lag"] <= hi]
        if not sel:
            continue
        a = [r["alpha"] for r in sel]
        sa = [r["salpha"] for r in sel if r["salpha"] is not None]
        clo, chi = backtest._ci_clustered([(r["d0"][:7], r["alpha"]) for r in sel])
        out.append({"lo": lo, "hi": hi, "n": len(sel),
                    "med": statistics.median(a), "mean": statistics.fmean(a),
                    "smed": statistics.median(sa) if sa else None,
                    "clo": clo, "chi": chi,
                    "sig": bool(clo is not None and (clo > 0 or chi < 0)),
                    "late": lo > STATUTORY_DAYS})
    return out


def within_member(rows, min_trades: int = 15) -> dict:
    """Per member: rank correlation between how late a trade was filed and how
    well it did. Reported as a distribution, since one member proves nothing."""
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["member"], []).append(r)
    corrs = []
    for name, ts in by.items():
        if len(ts) < min_trades:
            continue
        lags = [t["lag"] for t in ts]
        alphas = [t["alpha"] for t in ts]
        if len(set(lags)) < 3:            # no spread in lag: nothing to correlate
            continue
        try:
            corrs.append((name, statistics.correlation(lags, alphas, method="ranked"),
                          len(ts)))
        except (statistics.StatisticsError, ValueError):
            continue
    if not corrs:
        return {"n": 0, "med": None, "share_pos": None, "members": []}
    vals = [c for _, c, _ in corrs]
    return {"n": len(corrs), "med": statistics.median(vals),
            "share_pos": sum(1 for v in vals if v > 0) / len(vals),
            "members": sorted(corrs, key=lambda p: -p[1])}


def late_vs_prompt(rows) -> dict:
    late = [r for r in rows if r["lag"] > STATUTORY_DAYS]
    prompt = [r for r in rows if r["lag"] <= STATUTORY_DAYS]
    def side(sel):
        if not sel:
            return {"n": 0, "med": None, "clo": None, "chi": None}
        clo, chi = backtest._ci_clustered([(r["d0"][:7], r["alpha"]) for r in sel])
        return {"n": len(sel), "med": statistics.median([r["alpha"] for r in sel]),
                "mean": statistics.fmean([r["alpha"] for r in sel]),
                "clo": clo, "chi": chi}
    return {"late": side(late), "prompt": side(prompt)}


def build(floor: int = 1, cfg=CONFIG) -> dict:
    rows, dropped = load(floor, cfg)
    return {"n": len(rows), "dropped": dropped,
            "median_lag": statistics.median([r["lag"] for r in rows]) if rows else None,
            "buckets": by_bucket(rows),
            "within": within_member(rows),
            "split": late_vs_prompt(rows)}


def to_markdown(d: dict) -> str:
    p, r0 = scorecard.pct, scorecard.rate
    L = [f"# Filing lag vs alpha — does a member file their winners late?", "",
         f"{d['n']:,} priced disclosures with a usable transaction date "
         f"(median lag {d['median_lag']:.0f} days"
         + (f"; {d['dropped']} dropped for an unparseable or absurd date" if d["dropped"]
            else "") + ").", "",
         f"The STOCK Act allows {STATUTORY_DAYS} days. Intervals resample whole "
         "months, since disclosures cluster in time.", "",
         "Every return is measured from the **disclosure** date, so a trade filed a "
         "year late is scored on the stock a year after the member acted. Negative "
         "alpha in the late buckets says the disclosure had nothing actionable left "
         "in it, not that the member traded badly.", "",
         "| filing lag | disclosures | median α vs index | 90% by month "
         "| median α vs sector |",
         "|---|--:|--:|:--:|--:|"]
    for b in d["buckets"]:
        ci = "—" if b["clo"] is None else f"{p(b['clo'])} to {p(b['chi'])}"
        flag = " *(past deadline)*" if b["late"] else ""
        L.append(f"| {b['lo']}–{b['hi']} days{flag}{' **' if b['sig'] else ''} "
                 f"| {b['n']:,} | {p(b['med'])} | {ci} | {p(b['smed'])} |")

    s = d["split"]
    L += ["", "## Past the deadline vs within it", "",
          "| group | disclosures | median α | mean α | 90% by month |",
          "|---|--:|--:|--:|:--:|"]
    for k, label in (("prompt", f"within {STATUTORY_DAYS} days"),
                     ("late", f"past {STATUTORY_DAYS} days")):
        v = s[k]
        ci = "—" if v["clo"] is None else f"{p(v['clo'])} to {p(v['chi'])}"
        L.append(f"| {label} | {v['n']:,} | {p(v['med'])} | {p(v.get('mean'))} | {ci} |")

    w = d["within"]
    L += ["", "## Within each member's own history", ""]
    if not w["n"]:
        L.append("Not enough members with a spread of filing lags to test.")
    else:
        L += [f"For each of {w['n']} members with 15+ measurable trades, the rank "
              f"correlation between filing lag and alpha. Median **r = "
              f"{w['med']:+.2f}**, and {w['share_pos']*100:.0f}% of members are "
              "positive.",
              "",
              ("Near zero: within a member's own record, the later-filed trades are "
               "not the better ones. A population-level difference, if any, is about "
               "which members file late rather than which trades they file late."
               if abs(w["med"]) < 0.15 else
               "Non-trivial: worth looking at which members drive it."),
              "",
              "| member | r (lag vs alpha) | trades |", "|---|--:|--:|"]
        for name, c, n in (w["members"][:5] + w["members"][-5:]):
            L.append(f"| {name} | {c:+.2f} | {n} |")
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    rows, dropped = load(cfg=cfg)
    assert rows, "no rows with both dates and a closed window"
    assert all(0 <= r["lag"] <= MAX_SANE_LAG for r in rows), "insane lag survived"
    assert all(r["alpha"] is not None for r in rows)
    bs = by_bucket(rows)
    assert sum(b["n"] for b in bs) == len(rows), "buckets do not partition the rows"
    s = late_vs_prompt(rows)
    assert s["late"]["n"] + s["prompt"]["n"] == len(rows), "late/prompt split leaks"
    w = within_member(rows)
    assert w["med"] is None or -1 <= w["med"] <= 1
    txt = to_markdown(build(cfg=cfg))
    assert "Filing lag" in txt and "own history" in txt
    print(f"selftest ok: {len(rows):,} rows, {dropped} dropped, {len(bs)} buckets, "
          f"{w['n']} members correlated, within-member median r = {w['med']:+.2f}")
