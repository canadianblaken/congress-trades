"""MCP server: let an agent ask its own questions of the disclosure data.

Why this exists at all: the digest is one fixed slice -- 90 days, top 20 names --
and an agent that wants "Whitesides' aerospace sells" or "members who beat their
sector on 30-day windows" cannot get there from a static brief.

Why it is shaped defensively: 33,000 rows sliced arbitrarily is a
multiple-comparisons machine. An agent free to cut the data fifty ways will find
a member who "beats their sector 80% of the time" on twelve trades and report it
as a finding. So every response that carries a return also carries its sample
size and a month-clustered 90% interval, and `significant` is computed here
rather than left to the caller's judgement. There is no tool that returns a bare
average.

Runs over stdio, no dependencies beyond the standard library:

    python -m congress_trades.mcp_server

ponytail: hand-rolled JSON-RPC over stdio rather than an SDK. The protocol
surface used here is four methods; a framework would be more code than this file.
"""
from __future__ import annotations

import json
import statistics
import sys

from . import backtest, db, digest, lag, scorecard
from .config import CONFIG

MIN_REPORTABLE = 8          # below this, no interval is meaningful; say so instead


def _scored(rows, horizon="90"):
    out = []
    for r in rows:
        a = scorecard.alpha(r["tx_type"], r.get(f"ret_{horizon}"),
                            r.get(f"bench_{horizon}"))
        if a is not None:
            out.append((r, a))
    return out


def _stats(pairs) -> dict:
    """The only way a return leaves this server: with n, and with an interval."""
    if len(pairs) < MIN_REPORTABLE:
        return {"n": len(pairs),
                "note": f"fewer than {MIN_REPORTABLE} measurable trades; no "
                        "average is reported because none would be meaningful"}
    a = [x for _, x in pairs]
    clo, chi = backtest._ci_clustered([(r["d0"][:7], x) for r, x in pairs])
    months = len({r["d0"][:7] for r, _ in pairs})
    out = {"n": len(a),
           "median_alpha": round(statistics.median(a), 4),
           "mean_alpha": round(statistics.fmean(a), 4),
           "ci90_by_month": [round(clo, 4), round(chi, 4)] if clo is not None else None,
           "significant": bool(clo is not None and (clo > 0 or chi < 0)),
           "months": months,
           "benchmark": "SPY, same window, measured from the disclosure date"}
    if clo is None:
        # Enough trades to average but too few month clusters to bound. Saying
        # nothing here would hand back a bare number, which is the whole failure
        # this server exists to prevent.
        out["warning"] = (f"these {len(a)} trades span only {months} calendar "
                          "month(s), too few to resample, so no interval can be "
                          "computed and the averages above must not be quoted as a "
                          "result -- they describe this slice, they do not "
                          "generalise")
    return out


# ---------------------------------------------------------------- tools
def t_query_trades(member: str = "", ticker: str = "", sector: str = "",
                   tx_type: str = "", since: str = "", floor: int = 0,
                   limit: int = 50) -> dict:
    """Filtered disclosures plus the performance of exactly that slice."""
    floor = floor or CONFIG.default_floor
    with db.connect(CONFIG.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT t.member, t.chamber, t.ticker, t.tx_type, t.tx_date,
                      t.disclosed, t.amount_range, t.amount_min, t.owner,
                      COALESCE(s.sector,'') AS sector,
                      COALESCE(NULLIF(t.disclosed,''), t.tx_date) AS d0,
                      r.ret_90, r.bench_90, r.sec_90
                 FROM congress_trades t
                 LEFT JOIN trade_returns r ON r.trade_id = t.id
                 LEFT JOIN ticker_sectors s ON s.ticker = t.ticker
                WHERE t.ticker != '' AND t.amount_min >= ?
                  AND (? = '' OR t.member LIKE '%'||?||'%')
                  AND (? = '' OR t.ticker = ?)
                  AND (? = '' OR s.sector LIKE '%'||?||'%')
                  AND (? = '' OR t.tx_type = ?)
                  AND (? = '' OR COALESCE(NULLIF(t.disclosed,''),t.tx_date) >= ?)
                ORDER BY d0 DESC""",
            (floor, member, member, ticker, ticker, sector, sector,
             tx_type, tx_type, since, since))]
    pairs = _scored(rows)
    return {"matched": len(rows), "performance_of_this_slice": _stats(pairs),
            "trades": [{k: r[k] for k in
                        ("member", "ticker", "tx_type", "tx_date", "disclosed",
                         "amount_range", "owner", "sector")} for r in rows[:limit]],
            "truncated": max(0, len(rows) - limit)}


def t_member_scorecard(member: str = "", limit: int = 25) -> dict:
    """Benchmark-adjusted records. Carries the persistence figure every time,
    because the ranking is meaningless without it."""
    rows = scorecard.members(cfg=CONFIG)
    ps = scorecard.persistence(rows)
    if member:
        rows = [m for m in rows if member.lower() in m["member"].lower()]
    return {
        "persistence": {
            "rank_correlation_first_vs_second_half": (round(ps["r"], 3)
                                                      if ps["r"] is not None else None),
            "members": ps["n"],
            "share_same_sign": round(ps["same_sign"], 3) if ps["same_sign"] else None,
            "reading": ("near zero: a member's past alpha does not predict their next "
                        "trade, so this ranking is history and not a list to follow"
                        if ps["r"] is not None and abs(ps["r"]) < 0.25
                        else "some persistence, still one sample"),
        },
        "members_returned": len(rows[:limit]),
        "scorecard": [{
            "member": m["member"], "chamber": m["chamber"],
            "scored_trades": m["overall"]["n"],
            "median_alpha_vs_index": m["overall"]["med"],
            "median_alpha_vs_sector": m["overall"]["smed"],
            "beat_index_rate": m["overall"]["beat"],
            "first_half": m["split"]["first"]["med"],
            "second_half": m["split"]["second"]["med"],
            "top_ticker": m["conc"]["top"],
            "top_ticker_share": m["conc"]["share"],
            "one_bet_warning": bool(m["conc"]["share"] and m["conc"]["share"] >= 0.5),
        } for m in rows[:limit]],
    }


def t_run_backtest(split: str = "", walk_forward: bool = False) -> dict:
    """The out-of-sample test. Member selection uses only data before the cut."""
    if walk_forward:
        d = backtest.walk_forward(cfg=CONFIG)
    else:
        d = backtest.run(split or backtest.SPLIT, cfg=CONFIG)
    for v in d.get("strategies", {}).values():
        v.pop("lo", None)          # the naive interval is not for agents to quote
        v.pop("hi", None)
    d["reading"] = ("significance is decided by the month-clustered interval; a "
                    "strategy whose interval spans zero has not been shown to beat "
                    "the index, whatever its mean")
    return d


def t_filing_lag() -> dict:
    """Whether promptly disclosed trades differ from late ones."""
    d = lag.build(cfg=CONFIG)
    return {"median_lag_days": d["median_lag"], "measured": d["n"],
            "buckets": d["buckets"], "late_vs_prompt": d["split"],
            "within_member": {k: d["within"][k]
                              for k in ("n", "med", "share_pos")},
            "reading": ("returns are measured from the disclosure date, so late "
                        "buckets score a stale position -- read the gradient as how "
                        "actionable a disclosure still is, not as trading skill")}


def t_digest(days: int = 90) -> dict:
    """The standing brief: convergence, sector flow, committee overlap."""
    d = digest.build(days, CONFIG.default_floor, CONFIG)
    d.pop("_sectors_by_ticker", None)
    return d


TOOLS = {
    "query_trades": (t_query_trades, "Filtered disclosures plus the measured "
                     "performance of that exact slice, with a clustered interval.",
                     {"member": "string", "ticker": "string", "sector": "string",
                      "tx_type": "string (buy|sell)", "since": "string (ISO date)",
                      "floor": "integer", "limit": "integer"}),
    "member_scorecard": (t_member_scorecard, "Members ranked by alpha vs index and "
                         "vs their own sector; always includes the persistence "
                         "figure and one-bet concentration warnings.",
                         {"member": "string", "limit": "integer"}),
    "run_backtest": (t_run_backtest, "Out-of-sample test of following the "
                     "disclosures. Selection never sees the graded period.",
                     {"split": "string (ISO date)", "walk_forward": "boolean"}),
    "filing_lag": (t_filing_lag, "Alpha by how late the trade was disclosed.", {}),
    "digest": (t_digest, "Standing brief: convergence, sector flow, lone large "
               "positions, committee overlap.", {"days": "integer"}),
}

GUIDANCE = """\
This data cannot support confident stock picks, and the tools are built to make \
that visible rather than to stop you asking. Every figure arrives with its sample \
size and a 90% interval computed by resampling whole months, because these \
disclosures cluster in time and treating positions as independent overstates \
confidence badly. A result whose interval spans zero has not been shown; say so.

Established on this dataset, so do not re-derive it as news: member alpha does \
not persist between the halves of their own record; roughly two thirds of the \
apparent edge is sector exposure rather than stock selection; and no backtested \
strategy has a clustered interval that misses zero. Filings also lag trades by up \
to 45 days and amounts are reported brackets, never position sizes.

You are slicing 33,000 rows. If you cut them enough ways you will find something \
that looks significant; prefer a hypothesis stated before the query over a pattern \
found after it, and report how many slices you tried.\
"""


def _result(rid, payload):
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def handle(msg: dict):
    m, rid = msg.get("method"), msg.get("id")
    if m == "initialize":
        return _result(rid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "congress-trades", "version": "0.1",
                           "instructions": GUIDANCE}})
    if m == "tools/list":
        return _result(rid, {"tools": [
            {"name": n, "description": desc,
             "inputSchema": {"type": "object",
                             "properties": {k: {"type": v.split()[0]}
                                            for k, v in args.items()}}}
            for n, (_, desc, args) in TOOLS.items()]})
    if m == "tools/call":
        p = msg.get("params") or {}
        name = p.get("name")
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"no such tool: {name}"}}
        try:
            out = TOOLS[name][0](**(p.get("arguments") or {}))
        except Exception as e:                      # a bad argument must not kill the server
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"{type(e).__name__}: {e}"}}
        return _result(rid, {"content": [{"type": "text",
                                          "text": json.dumps(out, default=str)}]})
    if m in ("notifications/initialized", "initialized"):
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"unknown method: {m}"}}


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def selftest(cfg=CONFIG):
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["instructions"], "no guidance sent"
    listed = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == set(TOOLS), names

    def call(name, **kw):
        r = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": name, "arguments": kw}})
        assert "error" not in r, r
        return json.loads(r["result"]["content"][0]["text"])

    # Every slice that reports a return must report n and an interval with it.
    q = call("query_trades", ticker="NVDA", limit=3)
    perf = q["performance_of_this_slice"]
    assert "n" in perf
    assert "median_alpha" not in perf or "ci90_by_month" in perf, \
        "a return escaped without its interval"

    # A slice too narrow to bound must say so, not hand back a bare average.
    narrow = call("query_trades", member="Whitesides", tx_type="sell")
    np_ = narrow["performance_of_this_slice"]
    if np_.get("median_alpha") is not None and np_.get("ci90_by_month") is None:
        assert "warning" in np_, "unbounded average returned with no warning"
    assert len(q["trades"]) <= 3

    # A deliberately tiny slice must refuse to average rather than flatter itself.
    tiny = call("query_trades", member="Pelosi", ticker="BE")
    tp = tiny["performance_of_this_slice"]
    assert tp["n"] < MIN_REPORTABLE or "median_alpha" in tp
    if tp["n"] < MIN_REPORTABLE:
        assert "median_alpha" not in tp and "note" in tp, "small slice still averaged"

    sc = call("member_scorecard", limit=3)
    assert sc["persistence"]["rank_correlation_first_vs_second_half"] is not None
    assert len(sc["scorecard"]) <= 3

    bt = call("run_backtest")
    assert all("lo" not in v for v in bt["strategies"].values()), "naive CI leaked"

    lg = call("filing_lag")
    assert lg["buckets"] and "reading" in lg

    bad = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "nope"}})
    assert "error" in bad
    print(f"selftest ok: {len(TOOLS)} tools, guidance sent, "
          f"NVDA slice n={perf['n']}, tiny slice n={tp['n']} "
          f"({'refused' if 'note' in tp else 'averaged'})")


if __name__ == "__main__":
    serve()
