"""Chokepoint-stressindex ur PortWatchs dagliga transitserier.

Metod (ren aritmetik, inga beroenden):

  1. Metrik: daglig LASTKAPACITET (DWT) genom sundet — inte antal skrov.
     Ett sund kan ha oförändrat antal fartyg medan tonnaget halveras; det är
     tonnaget som bär ekonomin (samma val som atlas-earth gjorde).
  2. Baslinje per dag t: median över fönstret [t-365, t-45] dagar. Glappet på
     45 dagar hindrar en pågående störning från att äta sig in i sin egen
     baslinje på kort sikt.
  3. Robust z: (7d-medel − baslinje) / (1,4826 × MAD). MAD i stället för
     standardavvikelse — transitserier har feta svansar (helgdagar, stormar).
  4. Riktning: för vanliga sund är FALL i trafik stress; för inversa noder
     (Godahoppsudden = omdirigeringsmottagare) är STIGNING stress.
  5. FRUSEN BASLINJE: när riktad z ≥ START_Z startar en episod och baslinjen
     FRYSER på sitt förkrisvärde tills serien återhämtat sig (riktad z mot den
     frusna baslinjen < EXIT_Z i RECOVERY_DAYS dagar i rad). Utan detta
     normaliseras en långvarig katastrof av sin egen baslinje: Hormuz-
     stängningen 2026 (−97 % i sex månader) gled tillbaka till "normalt"
     eftersom det rullande fönstret hann absorbera kollapsen. Med frysning
     är en pågående störning stressad tills den faktiskt är över — och hela
     förloppet blir EN episod, inte flera brusiga.
  6. Stresspoäng 0-100: 50 + 10 × riktad z, klampad. 50 = normalt,
     ≥65 = förhöjt, ≥80 = allvarligt.

PortWatch släpar ~4-5 dygn — "senaste 7d-medel" är de sju färskaste dagarna
som FINNS i serien, inte kalenderns senaste sju.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

WINDOW_DAYS = 365       # baslinjefönstrets längd
GAP_DAYS = 45           # glapp mellan baslinjefönster och nuet
CURRENT_DAYS = 7        # medel över de senaste dagarna med data
MIN_BASELINE = 90       # färre baslinjedagar än så → ingen bedömning
Z_SCALE = 10.0          # stresspoäng per riktad z-enhet

START_Z = 1.5           # riktad z som startar en episod
EXIT_Z = 0.75           # under detta (mot frusen baslinje) räknas som återhämtat
RECOVERY_DAYS = 7       # dagar i rad under EXIT_Z innan episoden stängs
MIN_EPISODE_DAYS = 3    # kortare episoder än så rapporteras inte (brus)

LEVELS = [(80.0, "allvarligt"), (65.0, "förhöjt"), (0.0, "normalt")]


def series_by_name(rows: list[dict], metric: str = "capacity") -> dict[str, list[tuple[str, float]]]:
    """PortWatch-rader → {portname: [(date, värde), ...]} sorterat på datum."""
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        v = r.get(metric)
        d = r.get("date")
        if d and isinstance(v, (int, float)):
            out[r.get("portname", "")].append((d, float(v)))
    for name in out:
        out[name].sort()
    return dict(out)


def _robust_params(values: list[float]) -> tuple[float, float] | None:
    """(median, robust skala) för ett baslinjefönster, eller None om oanvändbart."""
    if len(values) < MIN_BASELINE:
        return None
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    scale = 1.4826 * mad
    if scale <= 0:
        try:
            scale = statistics.stdev(values)
        except statistics.StatisticsError:
            return None
        if scale <= 0:
            return None
    return med, scale


def classify(score: float) -> str:
    for threshold, label in LEVELS:
        if score >= threshold:
            return label
    return "normalt"


def walk(series: list[tuple[str, float]], inverse: bool = False) -> dict:
    """Gå igenom serien dag för dag med frusen-baslinje-logiken.

    Returnerar {"daily": [(date, dz)], "episodes": [...], "frozen": {...}|None}
    där varje episod är {start, end|None, days, peak_z, peak_date, ongoing}.
    """
    daily: list[tuple[str, float]] = []
    episodes: list[dict] = []
    frozen: dict | None = None      # {med, scale, start}
    calm_streak = 0
    current_ep: dict | None = None

    start_idx = MIN_BASELINE + GAP_DAYS
    for idx in range(start_idx, len(series)):
        date = series[idx][0]
        cur_window = [v for _, v in series[max(0, idx - CURRENT_DAYS + 1): idx + 1]]
        current = statistics.fmean(cur_window)

        if frozen is None:
            lo = max(0, idx - WINDOW_DAYS)
            hi = max(0, idx - GAP_DAYS)
            params = _robust_params([v for _, v in series[lo:hi]])
            if params is None:
                continue
            med, scale = params
        else:
            med, scale = frozen["med"], frozen["scale"]

        raw_z = (current - med) / scale
        dz = raw_z if inverse else -raw_z
        daily.append((date, dz))

        if frozen is None:
            if dz >= START_Z:
                # Episod startar: frys baslinjen på förkrisvärdet.
                frozen = {"med": med, "scale": scale, "start": date}
                current_ep = {"start": date, "end": None, "days": 1,
                              "peak_z": dz, "peak_date": date, "ongoing": True}
                calm_streak = 0
        else:
            current_ep["days"] += 1
            if dz > current_ep["peak_z"]:
                current_ep["peak_z"] = dz
                current_ep["peak_date"] = date
            if dz < EXIT_Z:
                calm_streak += 1
                if calm_streak >= RECOVERY_DAYS:
                    # Återhämtat: stäng episoden (slutet = första lugna dagen).
                    current_ep["end"] = daily[-RECOVERY_DAYS][0]
                    current_ep["days"] -= RECOVERY_DAYS - 1
                    current_ep["ongoing"] = False
                    if current_ep["days"] >= MIN_EPISODE_DAYS:
                        current_ep["peak_z"] = round(current_ep["peak_z"], 2)
                        episodes.append(current_ep)
                    frozen = None
                    current_ep = None
                    calm_streak = 0
            else:
                calm_streak = 0

    if current_ep is not None and current_ep["days"] >= MIN_EPISODE_DAYS:
        current_ep["peak_z"] = round(current_ep["peak_z"], 2)
        episodes.append(current_ep)

    return {"daily": daily, "episodes": episodes, "frozen": frozen}


def assess(series: list[tuple[str, float]], inverse: bool = False,
           walked: dict | None = None) -> dict | None:
    """Fullt stressläge för ett sund: poäng, nivå, avvikelse, sparkline.

    `walked` (från walk()) kan skickas in för att slippa räkna om.
    """
    if len(series) < MIN_BASELINE + GAP_DAYS + 1:
        return None
    w = walked or walk(series, inverse)
    if not w["daily"]:
        return None
    last_date, dz = w["daily"][-1]

    frozen = w["frozen"]
    if frozen is not None:
        baseline = frozen["med"]
    else:
        idx = len(series) - 1
        lo = max(0, idx - WINDOW_DAYS)
        hi = max(0, idx - GAP_DAYS)
        params = _robust_params([v for _, v in series[lo:hi]])
        baseline = params[0] if params else None

    cur_window = [v for _, v in series[-CURRENT_DAYS:]]
    current = statistics.fmean(cur_window)
    score = max(0.0, min(100.0, 50.0 + Z_SCALE * dz))
    ongoing = next((e for e in w["episodes"] if e.get("ongoing")), None)
    spark = [{"date": d, "value": round(v)} for d, v in series[-120:]]
    return {
        "score": round(score, 1),
        "level": classify(score),
        "directed_z": round(dz, 2),
        "current_7d": round(current),
        "baseline_median": round(baseline) if baseline else None,
        "deviation_pct": round((current - baseline) / baseline * 100, 1) if baseline else None,
        "last_date": last_date,
        "inverse": inverse,
        "ongoing_since": ongoing["start"] if ongoing else None,
        "ongoing_days": ongoing["days"] if ongoing else None,
        "spark": spark,
    }
