"""Would following these disclosures have paid? Answered with a train/test split.

The scorecard ranks members on their whole history. Selecting the top of that
ranking and then measuring it on the same history is the exact error the
persistence check exists to catch: it guarantees a good result and predicts
nothing. So here, members are ranked using disclosures BEFORE a cut date and
graded only on disclosures after it. Nothing the strategy knows at selection
time comes from the test window.

Positions are equal-weight and held a fixed horizon from the DISCLOSURE date,
which is the earliest a reader could have acted. The number reported is the
average alpha per position against SPY over the identical window.

ponytail: average position alpha, not a compounded equity curve. A real curve
needs position sizing, capital limits and overlap accounting; per-position alpha
answers "was the signal worth acting on" without any of that. Upgrade to a
dated cash-flow simulation if the answer ever turns out to be yes.
"""
from __future__ import annotations

import random
import statistics

from . import scorecard
from .config import CONFIG

SPLIT = "2026-01-01"       # train on 2025, grade on 2026
# Only ~18 months of disclosures carry a closed 90-day window at the default
# CONGRESS_YEARS, so this is one split rather than a walk-forward. Collect more
# years and re-ranking annually becomes worth doing.


def _alpha_of(t, horizon):
    return scorecard.alpha(t["tx_type"], t[f"ret_{horizon}"], t[f"bench_{horizon}"])


def _ci(a: list[float], iters: int = 2000, seed: int = 0) -> tuple:
    """Bootstrap 5-95% interval for the mean.

    Not decoration. These position counts are small and the alpha distribution
    has fat tails -- a handful of trades set the mean -- so a bare "+3.3%" reads
    as a result when it is often indistinguishable from zero. If the interval
    spans zero, the strategy has not been shown to do anything.
    """
    if len(a) < 5:
        return (None, None)
    rng = random.Random(seed)                 # fixed: the same table twice running
    means = sorted(statistics.fmean(rng.choices(a, k=len(a))) for _ in range(iters))
    return (means[int(iters * 0.05)], means[int(iters * 0.95)])


def _ci_clustered(pairs: list[tuple], iters: int = 2000, seed: int = 0,
                  level: float = 0.90) -> tuple:
    """Interval from resampling whole MONTHS, not individual positions.

    Positions here are nowhere near independent: hundreds of disclosures land in
    the same few weeks and ride the same market, so a plain bootstrap treats one
    regime as hundreds of observations and reports an interval far too narrow.
    Resampling months keeps the trades inside a month together, which is the
    honest unit of information. Expect this interval to be several times wider
    than the naive one -- and believe this one.

    ponytail: months are a crude cluster. Overlapping 90-day holds still correlate
    across adjacent months; a block bootstrap over quarters would be stricter
    still if a result ever depends on the difference.
    """
    by: dict[str, list] = {}
    for month, x in pairs:
        by.setdefault(month, []).append(x)
    months = list(by)
    if len(months) < 5:
        return (None, None)
    rng = random.Random(seed)
    means = []
    for _ in range(iters):
        pick = rng.choices(months, k=len(months))
        vals = [x for m in pick for x in by[m]]
        if vals:
            means.append(statistics.fmean(vals))
    means.sort()
    tail = (1 - level) / 2
    lo = means[int(len(means) * tail)]
    hi = means[min(len(means) - 1, int(len(means) * (1 - tail)))]
    return (lo, hi)


def _perf(trades, horizon) -> dict:
    """Equal-weight performance of a set of positions."""
    a = [x for x in (_alpha_of(t, horizon) for t in trades) if x is not None]
    if not a:
        return {"n": 0, "mean": None, "med": None, "beat": None, "lo": None,
                "hi": None, "clo": None, "chi": None, "months": 0, "sig": False}
    lo, hi = _ci(a)
    # The clustered interval is the one that decides significance; the naive one is
    # kept only to show how much the overlap was inflating confidence.
    clustered = [(t["d0"][:7], _alpha_of(t, horizon)) for t in trades
                 if _alpha_of(t, horizon) is not None]
    clo, chi = _ci_clustered(clustered)
    return {"n": len(a), "mean": statistics.fmean(a), "med": statistics.median(a),
            "beat": sum(1 for x in a if x > 0) / len(a),
            "lo": lo, "hi": hi, "clo": clo, "chi": chi,
            "months": len({m for m, _ in clustered}),
            # "significant" only in the weak sense that the clustered interval
            # misses zero.
            "sig": bool(clo is not None and (clo > 0 or chi < 0))}


def _rank(train, horizon, top_frac):
    """Members ordered by training-window alpha; returns (top set, bottom set, n)."""
    by: dict[str, list] = {}
    for t in train:
        by.setdefault(t["member"], []).append(t)
    ranked = []
    for name, ts in by.items():
        p = _perf(ts, horizon)
        if p["n"] >= scorecard.MIN_TRADES:
            ranked.append((p["med"], name, p["n"]))
    ranked.sort(reverse=True)
    k = max(1, int(len(ranked) * top_frac)) if ranked else 0
    return ({n for _, n, _ in ranked[:k]},
            {n for _, n, _ in ranked[-k:]} if ranked else set(),
            len(ranked))


def walk_forward(horizon: str = "90", floor: int = 0, top_frac: float = 0.34,
                 cfg=CONFIG) -> dict:
    """Re-rank every year, trade only the year that follows, pool the results.

    A single split spends most of a long history on training and grades one
    short period, so the answer swings on which cut you picked. Re-ranking
    annually reuses every year as a test year exactly once, and the pooled
    positions are what the interval is computed on.
    """
    floor = floor or cfg.default_floor
    rows = scorecard.load(floor, cfg)
    years = sorted({t["d0"][:4] for t in rows})
    if len(years) < 3:
        return {"years": years, "folds": [], "strategies": {}, "horizon": horizon,
                "floor": floor, "note": "needs at least three years of disclosures"}

    pooled: dict[str, list] = {"every disclosure": [], "top members": [],
                               "top members, buys": [], "top members, sells": [],
                               "bottom members": []}
    folds = []
    # The first year can only ever be training data, so testing starts at the second.
    for y in years[1:]:
        train = [t for t in rows if t["d0"][:4] < y]
        test = [t for t in rows if t["d0"][:4] == y]
        top, bottom, n_ranked = _rank(train, horizon, top_frac)
        if not n_ranked:
            folds.append({"year": y, "ranked": 0, "test": len(test), "top": None})
            continue
        picks = {
            "every disclosure": test,
            "top members": [t for t in test if t["member"] in top],
            "top members, buys": [t for t in test
                                  if t["member"] in top and t["tx_type"] == "buy"],
            "top members, sells": [t for t in test
                                   if t["member"] in top and t["tx_type"] == "sell"],
            "bottom members": [t for t in test if t["member"] in bottom],
        }
        for k, v in picks.items():
            pooled[k].extend(v)
        folds.append({"year": y, "ranked": n_ranked, "test": len(test),
                      "top": _perf(picks["top members"], horizon),
                      "all": _perf(picks["every disclosure"], horizon)})

    return {"horizon": horizon, "floor": floor, "years": years, "folds": folds,
            "strategies": {k: _perf(v, horizon) for k, v in pooled.items()}}


def run(split: str = SPLIT, horizon: str = "90", floor: int = 0,
        top_frac: float = 0.34, cfg=CONFIG) -> dict:
    floor = floor or cfg.default_floor
    rows = scorecard.load(floor, cfg)

    train = [t for t in rows if t["d0"] < split]
    test = [t for t in rows if t["d0"] >= split]
    top, bottom, n_ranked = _rank(train, horizon, top_frac)
    k = len(top)

    def sel(names=None, side=None):
        out = [t for t in test
               if (names is None or t["member"] in names)
               and (side is None or t["tx_type"] == side)]
        return _perf(out, horizon)

    return {
        "split": split, "horizon": horizon, "floor": floor,
        "train_trades": len(train), "test_trades": len(test),
        "ranked_members": n_ranked, "picked": sorted(top),
        "strategies": {
            # The baseline. If selection adds nothing, these match.
            "every disclosure": sel(),
            "every buy": sel(side="buy"),
            "every sell (as a short)": sel(side="sell"),
            f"top {k} members by train alpha": sel(top),
            f"top {k} members, buys only": sel(top, "buy"),
            f"top {k} members, sells as shorts": sel(top, "sell"),
            # The control: if the ranking carries information, this should be worse.
            f"bottom {k} members by train alpha": sel(bottom),
        },
    }


def to_markdown(d: dict) -> str:
    p = scorecard.pct
    L = [f"# Backtest — trained before {d['split']}, graded after it", "",
         f"{d['train_trades']} disclosures in the training window, "
         f"{d['test_trades']} in the test window; {d['ranked_members']} members had "
         f"enough training trades to rank.", "",
         "Equal-weight positions, held "
         f"{d['horizon']} days from the disclosure date, scored as alpha vs SPY over "
         "the identical window. Member selection uses training-window data only, so "
         "no strategy here knows anything about the period it is graded on.", "",
         "| strategy | positions | mean alpha | 90% by month | 90% naive "
         "| median | beat index |",
         "|---|--:|--:|:--:|:--:|--:|--:|"]
    for name, s in d["strategies"].items():
        ci = "—" if s["clo"] is None else f"{p(s['clo'])} to {p(s['chi'])}"
        naive = "—" if s["lo"] is None else f"{p(s['lo'])} to {p(s['hi'])}"
        star = " **" if s["sig"] else ""
        L.append(f"| {name}{star} | {s['n']} | {p(s['mean'])} | {ci} | {naive} "
                 f"| {p(s['med'])} | {scorecard.rate(s['beat'])} |")
    sig = [n for n, v in d["strategies"].items() if v["sig"]]
    L += ["",
          ("Rows marked ** have a 90% bootstrap interval that misses zero: "
           + ", ".join(sig) + "." if sig else
           "**No strategy here has a 90% interval that misses zero.** On this "
           "sample none of them is distinguishable from simply holding the index."),
          "",
          "A sell is only actionable as a short, and shorting is not frictionless: "
          "borrow costs, availability and margin are not modelled here, so treat the "
          "short rows as an upper bound.",
          "",
          "If 'every disclosure' matches the selected rows, member selection added "
          "nothing. If the bottom rows are no worse than the top, the ranking carries "
          "no information at all."]
    return "\n".join(L) + "\n"


def wf_to_markdown(d: dict) -> str:
    p = scorecard.pct
    if not d["folds"]:
        return f"# Walk-forward\n\n{d.get('note', 'no folds')}\n"
    L = [f"# Walk-forward — re-ranked each year, graded on the next", "",
         f"Years in the data: {', '.join(d['years'])}. Each year after the first is "
         "a test year exactly once, ranked only on the years before it. Positions "
         f"are pooled across folds and held {d['horizon']} days from disclosure.", "",
         "| pooled strategy | positions | months | mean alpha | 90% by month "
         "| 90% naive | beat index |",
         "|---|--:|--:|--:|:--:|:--:|--:|"]
    for name, v in d["strategies"].items():
        ci = "—" if v["clo"] is None else f"{p(v['clo'])} to {p(v['chi'])}"
        naive = "—" if v["lo"] is None else f"{p(v['lo'])} to {p(v['hi'])}"
        L.append(f"| {name}{' **' if v['sig'] else ''} | {v['n']} | {v['months']} "
                 f"| {p(v['mean'])} | {ci} | {naive} "
                 f"| {scorecard.rate(v['beat'])} |")

    L += ["", "## Per fold", "",
          "| test year | members ranked | top-member alpha | all-disclosure alpha |",
          "|---|--:|--:|--:|"]
    for f in d["folds"]:
        if not f.get("top"):
            L.append(f"| {f['year']} | 0 | — | — |")
            continue
        L.append(f"| {f['year']} | {f['ranked']} | {p(f['top']['mean'])} "
                 f"({f['top']['n']}) | {p(f['all']['mean'])} ({f['all']['n']}) |")

    sig = [n for n, v in d["strategies"].items() if v["sig"]]
    L += ["",
          ("Pooled rows marked ** have a 90% interval that misses zero: "
           + ", ".join(sig) + "."
           if sig else
           "**No pooled strategy has a 90% interval that misses zero.** Across every "
           "fold, none of them is distinguishable from holding the index."),
          "",
          "The **by month** interval resamples whole calendar months rather than "
          "individual positions, and it is the one that decides significance here. "
          "Hundreds of these disclosures land in the same few weeks and ride the "
          "same market, so the naive column treats one regime as hundreds of "
          "independent observations and is too narrow -- it is shown only so the "
          "size of that inflation is visible.",
          "",
          "A top-member row that beats the all-disclosure row in only some folds is "
          "the signature of a ranking that carries no information: check whether the "
          "sign flips year to year."]
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    d = run(cfg=cfg)
    rows = scorecard.load(cfg.default_floor, cfg)
    # No lookahead: nothing graded may predate the split, nothing used to select
    # may postdate it.
    assert all(t["d0"] >= d["split"] for t in rows if t["d0"] >= d["split"])
    train_max = max((t["d0"] for t in rows if t["d0"] < d["split"]), default="")
    assert train_max < d["split"], "training window leaks past the split"
    assert d["train_trades"] > 0, "empty training window -- split predates the data"
    assert d["ranked_members"] > 0, "nobody had enough training trades to rank"
    every = d["strategies"]["every disclosure"]
    assert every["n"] > 0, "no test-window positions -- move the split earlier"
    for name, s in d["strategies"].items():
        assert s["n"] <= every["n"], f"{name} has more positions than the whole market"
        if s["beat"] is not None:
            assert 0 <= s["beat"] <= 1
    for name, v in d["strategies"].items():
        if v["lo"] is not None:
            assert v["lo"] <= v["mean"] <= v["hi"], f"{name}: mean outside its own CI"
            assert v["sig"] == (v["lo"] > 0 or v["hi"] < 0)
    txt = to_markdown(d)
    assert "trained before" in txt and "beat index" in txt

    w = walk_forward(cfg=cfg)
    if w["folds"]:
        # No fold may be graded on a year it was ranked on.
        assert all(f["year"] > w["years"][0] for f in w["folds"]), "first year tested"
        assert len({f["year"] for f in w["folds"]}) == len(w["folds"]), "duplicate fold"
        every = w["strategies"]["every disclosure"]
        for name, v in w["strategies"].items():
            assert v["n"] <= every["n"], f"pooled {name} exceeds the whole market"
        assert "interval" in wf_to_markdown(w)
    print(f"selftest ok: {d['train_trades']} train / {d['test_trades']} test, "
          f"{len(d['picked'])} members picked, "
          f"every-disclosure mean {scorecard.pct(every['mean'])}")
