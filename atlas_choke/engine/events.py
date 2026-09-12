"""Event-studie: historiska stressepisoder → efterföljande prisrörelser.

Episoderna kommer från stress.walk() (frusen-baslinje-logiken). Här:

  1. `forward_returns` mäter varje kopplat instruments avkastning +5/+10/+20
     HANDELSDAGAR efter episodens startdatum, mot närmaste handelsdag.
  2. `event_study` parar ihop dem per chokepoint och rapporterar varje episod
     för sig plus poolad statistik (median, p10, p90) med ärliga N.

ÄRLIGHET: N är litet (stora chokepoint-störningar är sällsynta — det är
därför de flyttar priser). Banden är empiriska analog-kvantiler, inte
regressionsprognoser, och märks så i UI:t.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left

HORIZONS = (5, 10, 20)  # handelsdagar framåt


def forward_returns(history: list[dict], start_date: str) -> dict[int, float]:
    """{horisont: avkastning i %} från närmaste handelsdag ≥ start_date.

    `history` är Yahoo-dagsserien [{date, close}] i datumordning.
    """
    dates = [row["date"] for row in history]
    i = bisect_left(dates, start_date)
    if i >= len(dates):
        return {}
    base = history[i]["close"]
    if not base:
        return {}
    out = {}
    for h in HORIZONS:
        j = i + h
        if j < len(history):
            out[h] = round((history[j]["close"] - base) / base * 100, 2)
    return out


def event_study(episodes: list[dict], histories: dict[str, list[dict]]) -> dict:
    """Episoder × instrument → per-episod-utfall + poolad statistik.

    histories: {symbol: [{date, close}, ...]}
    Returnerar {"episodes": [...], "pooled": {symbol: {h: {n, median, p10, p90}}}}.
    """
    per_episode = []
    pooled: dict[str, dict[int, list[float]]] = {
        s: {h: [] for h in HORIZONS} for s in histories}
    for ep in episodes:
        rets = {}
        for symbol, hist in histories.items():
            fr = forward_returns(hist, ep["start"])
            if fr:
                rets[symbol] = fr
                for h, v in fr.items():
                    pooled[symbol][h].append(v)
        per_episode.append({**ep, "returns": rets})

    pooled_stats: dict[str, dict[int, dict]] = {}
    for symbol, by_h in pooled.items():
        stats_h = {}
        for h, vals in by_h.items():
            if not vals:
                continue
            stats_h[h] = {
                "n": len(vals),
                "median": round(statistics.median(vals), 2),
                "p10": round(_quantile(vals, 0.10), 2),
                "p90": round(_quantile(vals, 0.90), 2),
                **_loo_hit_rate(vals),
            }
        if stats_h:
            pooled_stats[symbol] = stats_h
    return {"episodes": per_episode, "pooled": pooled_stats}


def _loo_hit_rate(vals: list[float]) -> dict:
    """Leave-one-out riktningsträff: hade analog-medianen (utan episoden själv)
    förutsagt episodens tecken rätt?

    Detta är facit-frågan en finansjury ställer. LOO är obligatoriskt — att
    utvärdera medianen mot punkter som ingår i den är att rätta sitt eget
    prov. Kräver ≥3 episoder; oavgjorda (prognos ~0) räknas inte som försök.
    """
    if len(vals) < 3:
        return {}
    hits = tries = 0
    for i, actual in enumerate(vals):
        others = vals[:i] + vals[i + 1:]
        pred = statistics.median(others)
        if abs(pred) < 0.05 or abs(actual) < 0.05:
            continue
        tries += 1
        if (pred > 0) == (actual > 0):
            hits += 1
    if tries == 0:
        return {}
    return {"hits": hits, "tries": tries}


def unconditional_stats(history: list[dict]) -> dict[int, dict]:
    """Basnivå: fördelningen av ALLA rullande h-dagarsavkastningar i serien.

    Jämförelsepunkten som gör analogbanden hederliga — utan den kan man inte
    se om episoderna faktiskt skiljer sig från slumpen.
    """
    closes = [r["close"] for r in history]
    out = {}
    for h in HORIZONS:
        rets = [(closes[i + h] - closes[i]) / closes[i] * 100
                for i in range(len(closes) - h) if closes[i]]
        if len(rets) < 30:
            continue
        out[h] = {
            "n": len(rets),
            "median": round(statistics.median(rets), 2),
            "p10": round(_quantile(rets, 0.10), 2),
            "p90": round(_quantile(rets, 0.90), 2),
        }
    return out


def _quantile(vals: list[float], q: float) -> float:
    """Enkel empirisk kvantil (linjär interpolation), utan numpy."""
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac
