"""Speak only when something crosses a bar.

The nightly job wrote a note every day whether or not anything happened, which
trains you to stop reading it. This emits nothing on a quiet day and says so.

What it deliberately does NOT alert on: "a member with a good record just bought
X". That is the most tempting alert to build and this project's own numbers say
it is noise -- member alpha does not persist between the halves of their own
record (r = -0.09), and top-ranked members underperformed a follow-everyone
baseline in four of five walk-forward years. An alert on a name's track record
would be dressing that up as a signal.

So the bars are the things the data does support, or that are simply facts:

  fresh      disclosed within 15 days of the trade. The one gradient that
             survived a clustered interval: prompt filings carry alpha, stale
             ones carry none. Freshness is a property of the filing, not a
             prediction about the filer.
  large      a disclosed bracket at $100k or above. Not a forecast, just size.
  unusual    at least 10x that member's own median trade -- the lone-large
             logic, scaled to the person rather than the market.
  converging a ticker newly crossing four distinct members inside 30 days.
             Re-fires only when the count rises.
  committee  a trade at $100k+ in a sector the member's own committee oversees.
             Reported as a fact about jurisdiction, not as evidence of anything.

Every alert fires once. Fingerprints live in the database so a re-run on the same
day is silent, which is what makes this safe to put on a timer.
"""
from __future__ import annotations

import datetime as dt

from . import db, legislators
from .config import CONFIG

FRESH_DAYS = 15          # qualifier: the lag band that carried alpha
BIG = 100_001            # qualifier: a notable bracket, but common
HUGE = 500_001           # bar: fires on size alone, rare enough to be worth it
UNUSUAL_MULT = 10
UNUSUAL_FLOOR = 50_000   # a 10x multiple means nothing if the member's median is
                         # the reporting floor, so the trade must also be real money
CONVERGE_MEMBERS = 4
CONVERGE_WINDOW = 30

# Freshness and size describe a disclosure; they do not make it remarkable on
# their own. Most filings are prompt and a $100k bracket is routine, so these
# annotate an alert that fired for another reason rather than firing one.
QUALIFIERS = ("filed", "large")


def _fingerprints(conn) -> set[str]:
    return {r["fingerprint"] for r in conn.execute("SELECT fingerprint FROM alerts_seen")}


def _remember(conn, fps: list[str]) -> None:
    now = db.utcnow()
    for f in fps:
        conn.execute("INSERT OR IGNORE INTO alerts_seen (fingerprint, first_seen) "
                     "VALUES(?,?)", (f, now))


def money(n):
    n = n or 0
    return f"${n/1e6:.1f}M" if n >= 1e6 else f"${round(n/1e3)}k" if n >= 1e3 else f"${n}"


def find(days: int = 14, cfg=CONFIG, record: bool = True) -> list[dict]:
    """Alerts from disclosures that appeared in roughly the last `days`."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out: list[dict] = []
    with db.connect(cfg.db_path) as conn:
        seen = _fingerprints(conn)
        rows = [dict(r) for r in conn.execute(
            """SELECT t.id, t.member, t.chamber, t.ticker, t.tx_type, t.tx_date,
                      t.disclosed, t.amount_min, t.amount_range, t.owner,
                      COALESCE(s.sector,'') AS sector,
                      COALESCE(NULLIF(t.disclosed,''), t.tx_date) AS d0
                 FROM congress_trades t
                 LEFT JOIN ticker_sectors s ON s.ticker = t.ticker
                WHERE t.ticker != ''
                  AND COALESCE(NULLIF(t.disclosed,''), t.tx_date) >= ?""", (since,))]

        # Each member's own typical size, over their whole history, so "unusual"
        # means unusual for them rather than merely large.
        med: dict[str, float] = {}
        for r in conn.execute(
                """SELECT member, amount_min FROM congress_trades
                    WHERE ticker != '' AND amount_min > 0"""):
            med.setdefault(r["member"], []).append(r["amount_min"])
        import statistics
        med = {k: statistics.median(v) for k, v in med.items() if v}

        seats = db.committees_by_member(conn)
        bio = {r["member"]: r["bioguide"] for r in conn.execute(
            "SELECT member, bioguide FROM congress_members")}

        for r in rows:
            reasons = []
            lag = None
            if r["tx_date"] and r["disclosed"]:
                try:
                    lag = (dt.date.fromisoformat(r["disclosed"])
                           - dt.date.fromisoformat(r["tx_date"])).days
                except ValueError:
                    lag = None
            amt = r["amount_min"] or 0
            if lag is not None and 0 <= lag <= FRESH_DAYS:
                reasons.append(f"filed in {lag}d")
            if amt >= HUGE:
                reasons.append("very large")
            elif amt >= BIG:
                reasons.append("large")
            m = med.get(r["member"])
            if m and amt >= UNUSUAL_FLOOR and amt >= m * UNUSUAL_MULT:
                reasons.append(f"{(amt/m):.0f}x their median")
            if amt >= BIG and r["sector"]:
                covered = set()
                for st in seats.get(bio.get(r["member"]) or "", []):
                    covered.update(legislators.sectors_for_committee(st.get("name") or ""))
                if r["sector"] in covered:
                    reasons.append("their committee's sector")

            # At least one reason must be a bar rather than a qualifier. Without
            # this, every promptly filed routine disclosure alerts -- which was
            # the first version's behaviour and produced five a day.
            if not any(not x.startswith(QUALIFIERS) for x in reasons):
                continue
            fp = f"trade:{r['id']}"
            if fp in seen:
                continue
            out.append({"kind": "trade", "fp": fp, "member": r["member"],
                        "ticker": r["ticker"], "tx_type": r["tx_type"],
                        "amount": r["amount_min"], "range": r["amount_range"],
                        "disclosed": r["disclosed"], "sector": r["sector"],
                        "why": reasons,
                        "line": f"{r['member']} {r['tx_type']} {r['ticker']} "
                                f"{money(r['amount_min'])} ({', '.join(reasons)})"})

        # Convergence is a property of a ticker, not of one filing.
        cw = (dt.date.today() - dt.timedelta(days=CONVERGE_WINDOW)).isoformat()
        for r in conn.execute(
                """SELECT ticker, COUNT(DISTINCT member) n FROM congress_trades
                    WHERE ticker != '' AND amount_min >= ?
                      AND COALESCE(NULLIF(disclosed,''), tx_date) >= ?
                    GROUP BY ticker HAVING n >= ?""",
                (CONFIG.default_floor, cw, CONVERGE_MEMBERS)):
            fp = f"converge:{r['ticker']}:{r['n']}"
            if fp in seen:
                continue
            out.append({"kind": "converge", "fp": fp, "ticker": r["ticker"],
                        "members": r["n"],
                        "line": f"{r['n']} members traded {r['ticker']} in "
                                f"{CONVERGE_WINDOW}d"})
        if record and out:
            _remember(conn, [a["fp"] for a in out])
    return out


def to_text(alerts: list[dict], days: int = 14) -> str:
    if not alerts:
        return (f"No new disclosure crossed a bar in the last {days} days. "
                "Nothing to report.\n")
    trades = [a for a in alerts if a["kind"] == "trade"]
    conv = [a for a in alerts if a["kind"] == "converge"]
    L = [f"# {len(alerts)} new alert(s)", ""]
    if trades:
        L += ["## Disclosures crossing a bar", ""]
        # One filing can carry twenty qualifying transactions, and printing each
        # buries everything else. Group a member's same-day disclosures and lead
        # with the largest; the fingerprints stay per-trade, so nothing is lost.
        groups: dict[tuple, list] = {}
        for a in trades:
            groups.setdefault((a["member"], a["disclosed"]), []).append(a)
        blocks = sorted(groups.items(),
                        key=lambda kv: -max(x["amount"] or 0 for x in kv[1]))
        for (member, disclosed), items in blocks:
            items.sort(key=lambda a: -(a["amount"] or 0))
            when = f" — disclosed {disclosed}" if disclosed else ""
            if len(items) == 1:
                L.append(f"- {items[0]['line']}{when}")
                continue
            total = sum(x["amount"] or 0 for x in items)
            L.append(f"- **{member}**, {len(items)} disclosures{when}, "
                     f"{money(total)}+ combined")
            for x in items[:4]:
                L.append(f"    - {x['tx_type']} {x['ticker']} "
                         f"{money(x['amount'])} ({', '.join(x['why'])})")
            if len(items) > 4:
                L.append(f"    - …and {len(items)-4} more")
    if conv:
        L += ["", "## Converging names", ""] + [f"- {a['line']}" for a in conv]
    L += ["",
          f"Bars: {money(HUGE)}+, {UNUSUAL_MULT}x the member's own median (and at "
          f"least {money(UNUSUAL_FLOOR)}), a {money(BIG)}+ trade in their own "
          f"committee's sector, or {CONVERGE_MEMBERS}+ members on one name in "
          f"{CONVERGE_WINDOW} days. Being filed promptly or merely being {money(BIG)}+ "
          "annotates an alert but never raises one.",
          "",
          "Deliberately not a bar: the filer's track record. Member alpha does not "
          "persist in this data (r = -0.09), so an alert on a good record would be "
          "noise wearing a signal's clothes."]
    return "\n".join(L) + "\n"


def headline(alerts: list[dict]) -> str:
    """One line for a phone notification."""
    if not alerts:
        return ""
    trades = [a for a in alerts if a["kind"] == "trade"]
    if trades:
        top = max(trades, key=lambda a: a["amount"] or 0)
        # Count filers rather than rows: "+28 more" reads as 28 events when it is
        # usually one member's single filing.
        others = len({(a["member"], a["disclosed"]) for a in trades}) - 1
        extra = f" (+{others} other filer(s))" if others > 0 else ""
        return f"{top['line']}{extra}"
    return f"{alerts[0]['line']}" + (f" +{len(alerts)-1} more" if len(alerts) > 1
                                     else "")


def selftest(cfg=CONFIG):
    # Fingerprints must not be written by a dry look, or a test run would silence
    # the real one.
    a1 = find(3650, cfg, record=False)
    a2 = find(3650, cfg, record=False)
    assert len(a1) == len(a2), "non-recording run still changed state"
    for a in a1:
        if a["kind"] == "trade":
            assert any(not x.startswith(QUALIFIERS) for x in a["why"]), \
                f"qualifiers alone fired an alert: {a['why']}"
    assert all(a["fp"] for a in a1), "alert without a fingerprint"
    assert len({a["fp"] for a in a1}) == len(a1), "duplicate fingerprints"
    txt = to_text(a1)
    assert ("Nothing to report" in txt) == (not a1)
    assert (headline(a1) != "") == bool(a1)
    empty = to_text([])
    assert "Nothing to report" in empty and headline([]) == ""
    print(f"selftest ok: {len(a1)} alerts over all history, "
          f"{sum(1 for a in a1 if a['kind']=='converge')} convergence, "
          "idempotent without recording")
