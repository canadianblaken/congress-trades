"""The JSON the app reads: trades, tickers, members, trends, summary stats."""
from __future__ import annotations

import bisect
import datetime as dt
import re
import sqlite3

from .. import annual
from .. import db
from .. import prices
from .. import scorecard
from ..config import CONFIG

# --- database summary --------------------------------------------------------

def stats() -> dict:
    db = CONFIG.db_path
    if not db.exists():
        return {"contact": bool(CONFIG.contact)}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        one = lambda q: (conn.execute(q).fetchone() or [0])[0]
        out = {
            "trades": one("SELECT COUNT(*) FROM congress_trades"),
            "members": one("SELECT COUNT(*) FROM congress_members"),
            "priced": one("SELECT COUNT(*) FROM trade_returns WHERE ret_90 IS NOT NULL"),
            "tickers": one("SELECT COUNT(DISTINCT ticker) FROM congress_trades WHERE ticker != ''"),
            "latest": one("SELECT MAX(disclosed) FROM congress_trades"),
            "contact": bool(CONFIG.contact),
        }
        conn.close()
        return out
    except sqlite3.Error as e:
        return {"error": str(e)}


# --- live data API -----------------------------------------------------------

SORTS = {"date": "COALESCE(NULLIF(t.disclosed,''), t.tx_date)", "tx": "t.tx_date",
         "member": "t.member", "ticker": "t.ticker", "amount": "t.amount_min",
         "ret": "r.ret_90", "alpha": "alpha(t.tx_type, r.ret_90, r.bench_90)"}


def _ro():
    conn = sqlite3.connect(f"file:{CONFIG.db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    # The one definition of "did this trade work", callable from SQL. Every query
    # here uses it instead of spelling the formula out: the copies this replaced
    # had drifted -- sorting ignored a sell's direction, and exchanges scored as buys.
    conn.create_function("alpha", 3, scorecard.alpha, deterministic=True)
    return conn


def api_trades(q: dict) -> dict:
    """Filtered, sorted, paged trades straight from sqlite -- this is what makes the
    explorer live: every control is a query, not a re-render of a baked payload."""
    if not CONFIG.db_path.exists():
        return {"rows": [], "total": 0, "note": "no database yet"}
    where, params = ["1=1"], []
    text = (q.get("q") or [""])[0].strip()
    if text:
        where.append("(t.member LIKE ? OR t.ticker LIKE ? OR t.asset_name LIKE ?)")
        params += [f"%{text}%"] * 3
    for key, col in (("member", "t.member"), ("ticker", "t.ticker"),
                     ("chamber", "t.chamber"), ("type", "t.tx_type"), ("owner", "t.owner")):
        v = (q.get(key) or [""])[0].strip()
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    floor = (q.get("floor") or ["0"])[0]
    if re.fullmatch(r"\d{1,9}", floor) and int(floor):
        where.append("t.amount_min >= ?")
        params.append(int(floor))
    since = (q.get("since") or [""])[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        where.append("COALESCE(NULLIF(t.disclosed,''), t.tx_date) >= ?")
        params.append(since)
    # A string bound, so "2026-02-31" closes a month whatever its length.
    until = (q.get("until") or [""])[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
        where.append("COALESCE(NULLIF(t.disclosed,''), t.tx_date) <= ?")
        params.append(until)
    if (q.get("tickered") or [""])[0] == "1":
        where.append("t.ticker != ''")

    sort = SORTS.get((q.get("sort") or ["date"])[0], SORTS["date"])
    desc = "ASC" if (q.get("dir") or ["desc"])[0] == "asc" else "DESC"
    try:
        limit = min(max(int((q.get("limit") or ["100"])[0]), 1), 500)
        offset = max(int((q.get("offset") or ["0"])[0]), 0)
    except ValueError:
        limit, offset = 100, 0

    sql_from = ("FROM congress_trades t LEFT JOIN trade_returns r ON r.trade_id = t.id "
                "LEFT JOIN congress_members m ON m.member = t.member WHERE " + " AND ".join(where))
    try:
        conn = _ro()
        total = conn.execute(f"SELECT COUNT(*) {sql_from}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT t.id, t.chamber, t.member, m.full_name, m.party, t.ticker,
                       t.asset_name, t.tx_type, t.tx_date, t.disclosed, t.amount_range,
                       t.amount_min, t.doc_url, r.ret_90, r.bench_90
                {sql_from} ORDER BY {sort} {desc} NULLS LAST LIMIT ? OFFSET ?""",
            [*params, limit, offset]).fetchall()
        out = [dict(r) for r in rows]
        conn.close()
    except sqlite3.Error as e:
        return {"rows": [], "total": 0, "error": str(e)}
    for r in out:
        r["alpha"] = scorecard.alpha(r["tx_type"], r["ret_90"], r["bench_90"])
    return {"rows": out, "total": total, "limit": limit, "offset": offset}


def api_names() -> dict:
    """ticker -> a readable name, for hover text."""
    if not CONFIG.db_path.exists():
        return {}
    try:
        with _ro() as conn:
            return db.ticker_names(conn)
    except sqlite3.Error:
        return {}


def api_facets() -> dict:
    """The values the filter controls offer. Read once per load, so the explorer
    never invents a member or ticker that is not actually in this database."""
    if not CONFIG.db_path.exists():
        return {"members": [], "tickers": []}
    try:
        conn = _ro()
        members = [dict(r) for r in conn.execute(
            """SELECT t.member, COALESCE(m.full_name,'') full_name, COALESCE(m.party,'') party,
                      COALESCE(m.chamber,t.chamber) chamber, COUNT(*) n
                 FROM congress_trades t LEFT JOIN congress_members m ON m.member=t.member
                GROUP BY t.member ORDER BY n DESC""").fetchall()]
        tickers = [dict(r) for r in conn.execute(
            """SELECT ticker, COUNT(*) n FROM congress_trades WHERE ticker != ''
                GROUP BY ticker ORDER BY n DESC LIMIT 400""").fetchall()]
        conn.close()
        return {"members": members, "tickers": tickers}
    except sqlite3.Error as e:
        return {"members": [], "tickers": [], "error": str(e)}


def _price_trend(sym: str, marks: list[dict]) -> dict:
    """The cached close series for one name, bucketed to keep the line readable.

    Daily closes over ten years are 2,500 points in a 150px panel chart: past
    roughly a year the noise stops being information, so longer spans collapse to
    the last close of each week or month. The disclosures ride on the same
    buckets, which is the whole point of drawing them together -- you are looking
    for whether Congress moved before the line did."""
    s = prices.cached(sym)
    if not s:
        return {}
    days = sorted(d for d in s if isinstance(s.get(d), (int, float)))
    if len(days) < 8:
        return {}
    # Start a little before the first disclosure: the run-up is context, but the
    # decade of price history before anyone in Congress touched the name is not.
    first = min((m["d"] for m in marks if m["d"]), default="")
    if first:
        try:
            start = (dt.date.fromisoformat(first) - dt.timedelta(days=120)).isoformat()
            days = [d for d in days if d >= start] or days
        except ValueError:
            pass
    span = (dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days
    bucket = "day" if span <= 420 else "week" if span <= 2600 else "month"

    def key(d: str) -> str:
        if bucket == "day":
            return d
        if bucket == "month":
            return d[:7]
        day = dt.date.fromisoformat(d)
        return (day - dt.timedelta(days=day.weekday())).isoformat()

    closes: dict[str, tuple[str, float]] = {}
    for d in days:                         # last close wins, so each bucket ends where it ended
        closes[key(d)] = (d, float(s[d]))
    labels = sorted(closes)
    buys = [0] * len(labels)
    sells = [0] * len(labels)
    for m in marks:
        if not m["d"]:
            continue
        # A disclosure landing on a Saturday or a holiday has no bucket of its own,
        # so it attaches to the next one that exists -- the same forward walk the
        # return calculation uses. Exact matching silently drops a third of them.
        i = bisect.bisect_left(labels, key(m["d"]))
        if i >= len(labels):
            i = len(labels) - 1            # disclosed after the last cached close
        buys[i] += m["buys"] or 0
        sells[i] += m["sells"] or 0
    return {"bucket": bucket, "labels": labels,
            "close": [round(closes[k][1], 4) for k in labels],
            "asof": [closes[k][0] for k in labels],
            "buys": buys, "sells": sells}


def api_ticker(sym: str, q: dict) -> dict:
    """Everything known about one name, for the click-through from a chart bar.

    Deliberately not windowed the way the chart is: you arrive here asking what the
    whole record on this ticker looks like, and a panel that silently inherited the
    chart's date filter would answer a different question than the one asked."""
    sym = (sym or "").strip().upper()
    if not sym or not re.fullmatch(r"[A-Z0-9.\-]{1,12}", sym):
        return {"error": "bad symbol"}
    if not CONFIG.db_path.exists():
        return {"error": "no database"}
    try:
        conn = _ro()
        agg = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN amount_min ELSE 0 END) buy_vol,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 's%' THEN amount_min ELSE 0 END) sell_vol,
                      COUNT(DISTINCT member) members,
                      MIN(COALESCE(NULLIF(disclosed,''), tx_date)) first_seen,
                      MAX(COALESCE(NULLIF(disclosed,''), tx_date)) last_seen
                 FROM congress_trades WHERE ticker = ?""", (sym,)).fetchone()
        if not agg or not agg["n"]:
            conn.close()
            return {"ticker": sym, "agg": {"n": 0}}
        sector = conn.execute(
            "SELECT sector FROM ticker_sectors WHERE ticker = ?", (sym,)).fetchone()
        name = conn.execute(
            """SELECT asset_name FROM congress_trades
                WHERE ticker = ? AND asset_name != '' ORDER BY LENGTH(asset_name)
                LIMIT 1""", (sym,)).fetchone()
        # Median, not mean, to match how the scorecard reports a record.
        alphas = [r[0] for r in conn.execute(
            """SELECT alpha(t.tx_type, r.ret_90, r.bench_90)
                 FROM congress_trades t JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker = ?""",
            (sym,)).fetchall() if r[0] is not None]
        members = [dict(r) for r in conn.execute(
            """SELECT t.member, COALESCE(m.full_name,'') full_name,
                      COALESCE(m.party,'') party, COALESCE(m.chamber,t.chamber) chamber,
                      COUNT(*) n,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN t.amount_min
                               ELSE -t.amount_min END) net
                 FROM congress_trades t LEFT JOIN congress_members m ON m.member = t.member
                WHERE t.ticker = ? GROUP BY t.member ORDER BY n DESC LIMIT 15""",
            (sym,)).fetchall()]
        recent = [dict(r) for r in conn.execute(
            """SELECT t.member, COALESCE(m.full_name,'') full_name, COALESCE(m.party,'') party,
                      t.tx_type, t.tx_date,
                      t.disclosed, t.amount_range, t.doc_url, r.ret_90, r.bench_90
                 FROM congress_trades t LEFT JOIN congress_members m ON m.member = t.member
                 LEFT JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker = ?
                ORDER BY COALESCE(NULLIF(t.disclosed,''), t.tx_date) DESC LIMIT 12""",
            (sym,)).fetchall()]
        months = [dict(r) for r in conn.execute(
            """SELECT substr(COALESCE(NULLIF(disclosed,''), tx_date),1,7) ym,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells
                 FROM congress_trades WHERE ticker = ? AND ym != ''
                GROUP BY ym ORDER BY ym""", (sym,)).fetchall()]
        marks = [dict(r) for r in conn.execute(
            """SELECT COALESCE(NULLIF(disclosed,''), tx_date) d,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells
                 FROM congress_trades
                WHERE ticker = ? AND COALESCE(NULLIF(disclosed,''), tx_date) != ''
                GROUP BY d ORDER BY d""", (sym,)).fetchall()]
        conn.close()
    except sqlite3.Error as e:
        return {"error": str(e)}
    a = dict(agg)
    a["net"] = (a["buy_vol"] or 0) - (a["sell_vol"] or 0)
    med = None
    if alphas:
        alphas.sort()
        k = len(alphas)
        med = alphas[k // 2] if k % 2 else (alphas[k // 2 - 1] + alphas[k // 2]) / 2
    for r in recent:
        r["alpha"] = scorecard.alpha(r["tx_type"], r["ret_90"], r["bench_90"])
    return {"ticker": sym, "asset_name": name["asset_name"] if name else "",
            "sector": sector["sector"] if sector else "", "agg": a,
            "alpha_med": med, "alpha_n": len(alphas),
            "members": members, "recent": recent, "months": months,
            "price": _price_trend(sym, marks)}


def api_member(name: str) -> dict:
    """Everything known about one person, for the drill-down panel."""
    try:
        conn = _ro()
        row = conn.execute(
            "SELECT * FROM congress_members WHERE member = ?", (name,)).fetchone()
        prof = dict(row) if row else {}
        if prof.get("bioguide"):
            prof["committees"] = [dict(r) for r in conn.execute(
                "SELECT name, rank FROM member_committees WHERE bioguide = ?"
                if _has_col(conn, "member_committees", "rank") else
                "SELECT name FROM member_committees WHERE bioguide = ?",
                (prof["bioguide"],)).fetchall()]
        agg = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
                      SUM(t.amount_min) vol,
                      AVG(JULIANDAY(t.disclosed) - JULIANDAY(t.tx_date)) lag
                 FROM congress_trades t WHERE t.member = ?""", (name,)).fetchone()
        prof["agg"] = dict(agg) if agg else {}
        prof["top"] = [dict(r) for r in conn.execute(
            """SELECT ticker, COUNT(*) n,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN amount_min
                               WHEN LOWER(tx_type) LIKE 's%' THEN -amount_min
                               ELSE 0 END) net
                 FROM congress_trades
                WHERE member = ? AND ticker != '' GROUP BY ticker
                ORDER BY n DESC LIMIT 12""", (name,)).fetchall()]
        prof["annual"] = annual.member_summary(conn, name)
        conn.close()
        prof["compliance"] = _member_compliance(name)
        prof["finance"] = _member_finance(prof.get("bioguide"))
        prof["score"] = _member_score(name)
        return prof
    except sqlite3.Error as e:
        return {"error": str(e)}


# The other disclosure streams, joined onto one person. Each comes from the module
# that owns its rules -- compliance decides what counts as late, finance what counts
# as in-jurisdiction PAC money -- so the panel can never disagree with the report.
_whole: dict = {}


def _cached(name: str, fn):
    """A whole-dataset result, rebuilt only when the database file has changed."""
    stamp = CONFIG.db_path.stat().st_mtime if CONFIG.db_path.exists() else 0
    hit = _whole.get(name)
    if not hit or hit[0] != stamp:
        try:
            hit = _whole[name] = (stamp, fn())
        except Exception:                      # a report that cannot build is absent, not fatal
            hit = _whole[name] = (stamp, None)
    return hit[1]


def _member_compliance(name: str) -> dict | None:
    from .. import compliance
    d = _cached("compliance", lambda: compliance.build(CONFIG))
    m = ((d or {}).get("members") or {}).get(name)
    return {k: m.get(k) for k in ("total_trades", "late_count", "late_share",
                                  "worst_lag", "median_late_lag")} if m else None


def _member_score(name: str) -> dict:
    """The scoreboard's own figures for this member, or why there are none. The
    panel used to average alpha itself -- a mean over every trade, where the
    scoreboard takes a median over closed windows above the default floor -- and
    the two could disagree in sign for the same person."""
    d = _cached("scorecard", lambda: {m["member"]: m for m in scorecard.members(
        CONFIG.default_floor, "90", cfg=CONFIG)}) or {}
    m = d.get(name)
    if not m:
        return {"min": scorecard.MIN_TRADES}
    o = m["overall"]
    return {"med": o["med"], "smed": o["smed"], "beat": o["beat"], "n": o["n"]}


def _member_finance(bioguide: str | None) -> dict | None:
    from .. import finance
    if not bioguide:
        return None
    d = _cached("finance", lambda: finance.run(None, CONFIG))
    return next((o for o in (d or {}).get("overlaps") or []
                 if o.get("bioguide") == bioguide), None)


TOP_METRICS = {
    "net":     "ABS(buy_vol - sell_vol)",
    "volume":  "(buy_vol + sell_vol)",
    "count":   "n",
    "members": "members",
}

DATED = "COALESCE(NULLIF(t.disclosed,''), t.tx_date)"


def _window(q: dict) -> tuple[list[str], list]:
    """Shared filters for the aggregate views: window, floor, chamber."""
    where, params = ["t.ticker != ''"], []
    days = (q.get("days") or ["365"])[0]
    if re.fullmatch(r"\d{1,5}", days) and int(days):
        cut = (dt.date.today() - dt.timedelta(days=int(days))).isoformat()
        where.append(f"{DATED} >= ?")
        params.append(cut)
    floor = (q.get("floor") or ["0"])[0]
    if re.fullmatch(r"\d{1,9}", floor) and int(floor):
        where.append("t.amount_min >= ?")
        params.append(int(floor))
    ch = (q.get("chamber") or [""])[0]
    if ch in ("House", "Senate"):
        where.append("t.chamber = ?")
        params.append(ch)
    return where, params


def api_top(q: dict) -> dict:
    """Most-traded names, by whichever measure the reader asked for.

    Net flow is buying minus selling, so it is ordered by absolute size: the point
    is what moved, and a name Congress dumped is as informative as one it bought.
    Amounts are disclosed brackets, so every dollar figure is a floor, never exact."""
    if not CONFIG.db_path.exists():
        return {"rows": [], "metric": "net"}
    metric = (q.get("metric") or ["net"])[0]
    order = TOP_METRICS.get(metric, TOP_METRICS["net"])
    where, params = _window(q)
    try:
        limit = min(max(int((q.get("limit") or ["18"])[0]), 3), 40)
    except ValueError:
        limit = 18
    sql = f"""
        SELECT ticker,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN t.amount_min ELSE 0 END) buy_vol,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN t.amount_min ELSE 0 END) sell_vol,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
               COUNT(*) n, COUNT(DISTINCT t.member) members
          FROM congress_trades t
         WHERE {' AND '.join(where)}
         GROUP BY ticker HAVING n > 0
         ORDER BY {order} DESC LIMIT ?"""
    try:
        conn = _ro()
        rows = [dict(r) for r in conn.execute(sql, [*params, limit]).fetchall()]
        conn.close()
    except sqlite3.Error as e:
        return {"rows": [], "error": str(e)}
    for r in rows:
        r["net"] = (r["buy_vol"] or 0) - (r["sell_vol"] or 0)
        r["volume"] = (r["buy_vol"] or 0) + (r["sell_vol"] or 0)
        r["count"] = r["n"]
    return {"rows": rows, "metric": metric if metric in TOP_METRICS else "net"}


def api_timeline(q: dict) -> dict:
    """Buying and selling per month -- the shape of the trend behind the top names."""
    if not CONFIG.db_path.exists():
        return {"rows": []}
    where, params = _window(q)
    where[0] = "1=1"                       # the timeline counts untickered rows too
    sql = f"""
        SELECT substr({DATED},1,7) ym,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN t.amount_min ELSE 0 END) buy_vol,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN t.amount_min ELSE 0 END) sell_vol,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
               COUNT(*) n, COUNT(DISTINCT t.member) members
          FROM congress_trades t
         WHERE {' AND '.join(where)} AND substr({DATED},1,7) != ''
         GROUP BY ym ORDER BY ym"""
    try:
        conn = _ro()
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()
        # The window's first month is usually partial; a click-through needs the cut.
        cut = next((p for w, p in zip(where[1:], params) if w.startswith(DATED)), "")
        return {"rows": rows, "since": cut}
    except sqlite3.Error as e:
        return {"rows": [], "error": str(e)}


def _has_col(conn, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


