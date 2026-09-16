"""STOCK Act late-filing compliance tracker.

The STOCK Act requires reporting within 30 days of notification and no later than
45 days after the transaction date. Only the 45-day outer bound is checkable from
this data; notification dates are not disclosed.

A late filing is disclosed - tx_date > 45 days. This is a fact about the dates
on the filing, not a finding of wrongdoing. Whether the $200 fee was assessed or
waived is not public, so this cannot report violations or penalties—only late
filings.

Source data contains real garbage: a notification date of 03/28/1935 has been
observed. Negative lags or lags of many thousands of days indicate bad source
data rather than spectacular violations and are excluded or separately flagged.
"""
from __future__ import annotations

import datetime as dt
import statistics

from . import db
from .config import CONFIG

STATUTORY_DAYS = 45
MAX_SANE_LAG = 1095  # ~3 years; beyond this signals bad data


def load(cfg=CONFIG):
    """Load all trades with both transaction and disclosure dates."""
    with db.connect(cfg.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT id, member, chamber, state, ticker, asset_name, tx_type,
                      tx_date, disclosed, amount_min
                 FROM congress_trades
                WHERE tx_date != '' AND disclosed != ''
                ORDER BY member, tx_date DESC""")]

    out, dropped = [], 0
    for r in rows:
        try:
            lag = (dt.date.fromisoformat(r["disclosed"])
                   - dt.date.fromisoformat(r["tx_date"])).days
        except ValueError:
            dropped += 1
            continue

        # Flag and exclude absurd data.
        if lag < 0 or lag > MAX_SANE_LAG:
            dropped += 1
            continue

        r["lag"] = lag
        r["is_late"] = lag > STATUTORY_DAYS
        out.append(r)

    return out, dropped


def by_member(rows) -> dict[str, dict]:
    """Aggregate per-member statistics.

    For each member, count late filings, compute share of total, median and worst
    lag, and total notional amount disclosed late.
    """
    by_member: dict[str, list] = {}
    for r in rows:
        by_member.setdefault(r["member"], []).append(r)

    out = {}
    for name, trades in by_member.items():
        late = [t for t in trades if t["is_late"]]
        lags = [t["lag"] for t in trades]
        late_lags = [t["lag"] for t in late]

        out[name] = {
            "member": name,
            "chamber": trades[0]["chamber"],
            "state": trades[0]["state"],
            "total_trades": len(trades),
            "late_count": len(late),
            "late_share": len(late) / len(trades) if trades else 0,
            "median_lag": statistics.median(lags) if lags else None,
            "worst_lag": max(lags) if lags else None,
            "median_late_lag": statistics.median(late_lags) if late_lags else None,
            "amount_min_late": sum(t["amount_min"] for t in late),
        }

    return out


def extreme_filings(rows, limit: int = 20) -> list[dict]:
    """Find the most extreme individual late filings, ranked by lag."""
    late = [r for r in rows if r["is_late"]]
    late_sorted = sorted(late, key=lambda t: -t["lag"])

    out = []
    for t in late_sorted[:limit]:
        out.append({
            "member": t["member"],
            "chamber": t["chamber"],
            "ticker": t["ticker"],
            "asset_name": t["asset_name"],
            "tx_date": t["tx_date"],
            "disclosed": t["disclosed"],
            "lag_days": t["lag"],
            "amount_min": t["amount_min"],
        })

    return out


def build(cfg=CONFIG) -> dict:
    """Build the full compliance report."""
    rows, dropped = load(cfg)
    by_mem = by_member(rows)

    # Rank members by late filing count (and break ties by member name).
    ranked = sorted(by_mem.values(),
                    key=lambda m: (-m["late_count"], m["member"]))

    return {
        "total_trades": len(rows),
        "dropped": dropped,
        "total_late": sum(1 for r in rows if r["is_late"]),
        "members": by_mem,
        "ranked_members": ranked,
        "extreme_filings": extreme_filings(rows),
    }


def to_markdown(d: dict) -> str:
    """Render the compliance report to markdown."""
    L = [
        "# STOCK Act Compliance: Late Filings (45-day rule)",
        "",
        "A **late filing** is a transaction disclosed more than 45 days after it "
        "occurred. This is a fact about the dates on the filing, not a finding of "
        "wrongdoing. The STOCK Act nominally gives 30 days from notification, but "
        "notification dates are not disclosed; only the 45-day outer bound is "
        "checkable. Whether the $200 late-filing fee was assessed or waived is not "
        "public.",
        "",
        f"{d['total_trades']:,} trades with both transaction and disclosure dates. "
        f"{d['total_late']:,} ({d['total_late'] * 100 / d['total_trades']:.1f}%) "
        "exceed the 45-day window."
        + (f" {d['dropped']} rows with unparseable or absurd dates were excluded."
           if d["dropped"] else ""),
        "",
        "## Members by late-filing count",
        "",
        "| member | chamber | late count | of total | median lag | worst lag | "
        "amount late |",
        "|---|---|--:|--:|--:|--:|--:|",
    ]

    for m in d["ranked_members"]:
        if m["late_count"] == 0:
            continue
        pct = f"{m['late_share'] * 100:.1f}%" if m["late_share"] is not None else "—"
        median_lag = f"{m['median_lag']:.0f}" if m["median_lag"] is not None else "—"
        worst_lag = f"{m['worst_lag']:.0f}" if m["worst_lag"] is not None else "—"
        amount = f"${m['amount_min_late']:,}" if m["amount_min_late"] else "—"
        L.append(f"| {m['member']} | {m['chamber'][:1]} | {m['late_count']} "
                 f"| {pct} | {median_lag} days | {worst_lag} days | {amount} |")

    L += [
        "",
        "## Most extreme individual late filings",
        "",
        "| member | ticker | tx date | disclosed | lag (days) | amount |",
        "|---|---|---|---|--:|--:|",
    ]

    for t in d["extreme_filings"]:
        ticker = t["ticker"] or t["asset_name"] or "—"
        amount = f"${t['amount_min']:,}" if t["amount_min"] else "—"
        L.append(f"| {t['member']} | {ticker} | {t['tx_date']} | "
                 f"{t['disclosed']} | {t['lag_days']} | {amount} |")

    L += [
        "",
        "## Data quality notes",
        "",
        "- Negative lags and lags exceeding ~3 years indicate unparseable or "
        "nonsensical source dates (e.g., a notification date of 03/28/1935) and "
        "are excluded.",
        "- Filing dates come from the Clerk's index, not hand-typed PDF columns.",
        "- This measure cannot detect violations: only whether a filing crossed "
        "the 45-day threshold.",
    ]

    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    """Validate internal consistency of the compliance data."""
    rows, dropped = load(cfg)
    assert rows, "no rows with both dates"

    # Check lag calculations.
    for r in rows:
        assert 0 <= r["lag"] <= MAX_SANE_LAG, f"insane lag {r['lag']}"
        try:
            tx = dt.date.fromisoformat(r["tx_date"])
            dis = dt.date.fromisoformat(r["disclosed"])
            expected_lag = (dis - tx).days
            assert r["lag"] == expected_lag, \
                f"lag mismatch for {r['member']}: {r['lag']} vs {expected_lag}"
        except ValueError:
            raise AssertionError(f"unparseable dates in row {r}")

    # Check is_late classification.
    for r in rows:
        expected_late = r["lag"] > STATUTORY_DAYS
        assert r["is_late"] == expected_late, \
            f"is_late mismatch for {r['member']}: {r['is_late']} vs {expected_late}"

    by_mem = by_member(rows)

    # Build a member-to-trades mapping to verify aggregation.
    trades_by_mem: dict[str, list] = {}
    for r in rows:
        trades_by_mem.setdefault(r["member"], []).append(r)

    # Per-member aggregation.
    for name, mem_stats in by_mem.items():
        trades = trades_by_mem.get(name, [])

        # Count consistency.
        assert mem_stats["total_trades"] == len(trades), \
            f"{name}: total count mismatch"

        late_count = sum(1 for t in trades if t["is_late"])
        assert mem_stats["late_count"] == late_count, \
            f"{name}: late count mismatch"

        # Late share.
        if trades:
            expected_share = late_count / len(trades)
            assert abs(mem_stats["late_share"] - expected_share) < 1e-9, \
                f"{name}: late share mismatch"

        # Amount sum.
        late_amount = sum(t["amount_min"] for t in trades if t["is_late"])
        assert mem_stats["amount_min_late"] == late_amount, \
            f"{name}: amount sum mismatch"

    # Ranking consistency.
    report = build(cfg)
    assert report["total_trades"] == len(rows), "total trade count mismatch"
    assert report["total_late"] == sum(1 for r in rows if r["is_late"]), \
        "total late count mismatch"

    ranked = report["ranked_members"]
    assert all(m["late_count"] >= 0 for m in ranked), "negative late count"
    assert all(m["late_count"] <= m["total_trades"] for m in ranked), \
        "late count exceeds total"

    # Markdown output.
    txt = to_markdown(report)
    assert "STOCK Act" in txt, "title missing"
    assert "45-day" in txt, "statutory window not mentioned"
    assert "late-filing count" in txt or "late filing" in txt, "ranking not present"

    print(f"selftest ok: {len(rows):,} trades, {report['total_late']} late, "
          f"{dropped} dropped, {len(by_mem)} members")
