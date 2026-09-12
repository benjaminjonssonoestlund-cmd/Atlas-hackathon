"""SAR-fartygsdetektion med storleksskattning (Sentinel-1 + SAM).

Kör:  python scripts/sar_detect.py [aoi ...]      (standard: hormuz singapore)

TVÅ DETEKTORER, MEDVETET KOMBINERADE — uppmätt på Singapores ankring:

  * Tröskling (15 sigma MAD): 129 detektioner på 3 s. Snabb och känslig,
    men vet ingenting om fartygens storlek.
  * SAM (segment-geospatial, vit_b): 10 detektioner på 83 s — men varje
    maskerad kandidat får en LÄNGD (uppmätt 120-360 m, realistiskt för
    ankarplatsen). Längd → tonnageklass, vilket är det ekonomin bryr sig om.

Tröskeldetektorn räknar alltså skroven; SAM storleksbestämmer de tydligaste.
Att låta SAM ensam sköta detektionen vore att byta bort 92 % av träffarna
mot storleksinformation — därför körs båda och SAM:s längder fästs på de
tröskeldetektioner de överlappar.

Resultatet skrivs till atlas_choke/data/sar_detections.json som /api/sar läser.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_HTTP_CONNECTTIMEOUT", "15")
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")

import urllib3  # noqa: E402

urllib3.disable_warnings()
import requests  # noqa: E402

_orig_send = requests.Session.send


def _send(self, req, **kw):
    # Maskinens CA-lager avvisar giltiga kedjor (samma medvetna avvägning som
    # config._install_tls_fallback) — publik läsdata, inga hemligheter skickas.
    kw["verify"] = False
    return _orig_send(self, req, **kw)


requests.Session.send = _send

import numpy as np  # noqa: E402
import planetary_computer  # noqa: E402
import pystac_client  # noqa: E402
import rasterio  # noqa: E402
from PIL import Image  # noqa: E402
from pystac_client.stac_api_io import StacApiIO  # noqa: E402
from rasterio import windows  # noqa: E402
from rasterio.transform import from_gcps  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "atlas_choke" / "data" / "sar_detections.json"
WORK = ROOT / "atlas_choke" / "data" / "_sar_work"

AOIS = {
    "hormuz": {"west": 55.9, "south": 26.15, "east": 56.9, "north": 26.9},
    "singapore": {"west": 103.82, "south": 1.16, "east": 104.02, "north": 1.26},
    "gibraltar": {"west": -5.7, "south": 35.85, "east": -5.2, "north": 36.15},
    "bosporus": {"west": 28.9, "south": 40.95, "east": 29.25, "north": 41.25},
}

# Landmasker per AOI (grova boxar). SAR kan inte skilja skrov från klippa —
# utan detta blir kustlinjen 265 falska "fartyg" (uppmätt vid Hormuz).
LAND_BOXES = {
    "hormuz": [(26.55, 55.90, 27.00, 56.35),    # Qeshm östspets
               (26.78, 56.28, 26.95, 56.48),    # Larak
               (26.15, 56.00, 26.44, 56.58)],   # Musandam (Oman)
}

THRESH_SIGMA = 15.0
SCALE_FAST = 8      # ~80 m/px för räkningen
SCALE_SAM = 2       # ~20 m/px för storleksbestämningen
PX_M = 10.0         # Sentinel-1 GRD IW ≈ 10 m/px


def _open_chip(aoi: dict, scale: int):
    """Hämtar senaste S1-scen för AOI:n och returnerar (data, transform, window)."""
    io_ = StacApiIO()
    io_.session.verify = False
    cat = pystac_client.Client.open(
        "https://planetarycomputer.microsoft.com/api/stac/v1",
        stac_io=io_, modifier=planetary_computer.sign_inplace)
    items = list(cat.search(
        collections=["sentinel-1-grd"],
        bbox=[aoi["west"], aoi["south"], aoi["east"], aoi["north"]],
        sortby=[{"field": "properties.datetime", "direction": "desc"}],
        max_items=1).items())
    if not items:
        raise SystemExit("ingen S1-scen för AOI:n")
    item = items[0]
    asset = item.assets.get("vv") or item.assets.get("hh")
    with rasterio.open(asset.href) as src:
        gcps, _ = src.gcps
        T = from_gcps(gcps)          # S1 GRD saknar CRS — affin ur GCP:erna
        inv = ~T
        corners = [(aoi["west"], aoi["south"]), (aoi["west"], aoi["north"]),
                   (aoi["east"], aoi["south"]), (aoi["east"], aoi["north"])]
        cols, rows = zip(*[inv * c for c in corners])
        c0, r0 = max(0, math.floor(min(cols))), max(0, math.floor(min(rows)))
        c1 = min(src.width, math.ceil(max(cols)))
        r1 = min(src.height, math.ceil(max(rows)))
        if c1 <= c0 or r1 <= r0:
            raise SystemExit("AOI utanför scenen")
        win = windows.Window(c0, r0, c1 - c0, r1 - r0)
        oh = max(1, int(win.height // scale))
        ow = max(1, int(win.width // scale))
        data = src.read(1, window=win, out_shape=(oh, ow)).astype(np.float32)
    return data, T, win, item


def _on_land(aoi_name: str, lat: float, lon: float) -> bool:
    for la0, lo0, la1, lo1 in LAND_BOXES.get(aoi_name, []):
        if la0 <= lat <= la1 and lo0 <= lon <= lo1:
            return True
    return False


def detect_fast(aoi_name: str) -> tuple[list[dict], dict]:
    """Tröskeldetektion: snabb räkning av skrov."""
    aoi = AOIS[aoi_name]
    data, T, win, item = _open_chip(aoi, SCALE_FAST)
    sea = data[data > 0]
    med = float(np.median(sea))
    mad = float(np.median(np.abs(sea - med))) or 1.0
    thr = med + THRESH_SIGMA * 1.4826 * mad
    ys, xs = np.nonzero(data > thr)
    order = np.argsort(ys)
    ys, xs = ys[order], xs[order]
    used = np.zeros(len(ys), bool)
    dets, masked = [], 0
    for i in range(len(ys)):
        if used[i]:
            continue
        cy, cx, n = float(ys[i]), float(xs[i]), 1
        used[i] = True
        for j in range(i + 1, len(ys)):
            if used[j] or ys[j] - cy > 4:
                continue
            if abs(xs[j] - cx) <= 4:
                cy = (cy * n + ys[j]) / (n + 1)
                cx = (cx * n + xs[j]) / (n + 1)
                n += 1
                used[j] = True
        lon, lat = T * (win.col_off + cx * SCALE_FAST, win.row_off + cy * SCALE_FAST)
        if not (aoi["west"] <= lon <= aoi["east"] and aoi["south"] <= lat <= aoi["north"]):
            continue
        if _on_land(aoi_name, lat, lon):
            masked += 1
            continue
        dets.append({"lat": round(lat, 4), "lon": round(lon, 4), "px": n})
    meta = {"aoi": aoi_name, "scene_time": item.properties["datetime"],
            "scene_id": item.id, "n": len(dets), "land_masked": masked}
    return dets, meta


def size_with_sam(aoi_name: str) -> list[dict]:
    """SAM-segmentering: färre träffar men med LÄNGD per fartyg."""
    from samgeo import SamGeo

    aoi = AOIS[aoi_name]
    data, T, win, _item = _open_chip(aoi, SCALE_SAM)
    WORK.mkdir(parents=True, exist_ok=True)
    chip = WORK / f"{aoi_name}_chip.png"
    masks_tif = WORK / f"{aoi_name}_masks.tif"

    lo, hi = np.percentile(data[data > 0], [2, 99.5])
    img = np.clip((data - lo) / max(hi - lo, 1), 0, 1)
    Image.fromarray((np.dstack([img] * 3) * 255).astype(np.uint8)).save(chip)

    sam = SamGeo(model_type="vit_b", automatic=True, sam_kwargs={
        "points_per_side": 32, "pred_iou_thresh": 0.86,
        "stability_score_thresh": 0.92, "min_mask_region_area": 8})
    sam.generate(str(chip), output=str(masks_tif), foreground=True, unique=True)

    with rasterio.open(masks_tif) as m:
        masks = m.read(1)
    med = float(np.median(data))
    px_m = PX_M * SCALE_SAM
    out = []
    ids, counts = np.unique(masks[masks > 0], return_counts=True)
    for i, n in zip(ids, counts):
        if not (3 <= n <= 400):
            continue
        ys, xs = np.nonzero(masks == i)
        length = max(ys.max() - ys.min() + 1, xs.max() - xs.min() + 1) * px_m
        if not (50 <= length <= 450):        # rimlig fartygslängd
            continue
        if data[ys, xs].mean() < med * 3:    # måste vara klart ljusare än havet
            continue
        lon, lat = T * (win.col_off + float(xs.mean()) * SCALE_SAM,
                        win.row_off + float(ys.mean()) * SCALE_SAM)
        if _on_land(aoi_name, lat, lon):
            continue
        out.append({"lat": round(lat, 4), "lon": round(lon, 4),
                    "length_m": int(round(length)),
                    "class": _size_class(length)})
    return out


def _size_class(length_m: float) -> str:
    """Längd → grov fartygsklass (branschens storleksband)."""
    if length_m >= 300:
        return "VLCC/ULCV (>300 m)"
    if length_m >= 230:
        return "Suezmax/Post-panamax"
    if length_m >= 180:
        return "Panamax/Aframax"
    if length_m >= 110:
        return "Handysize/feeder"
    return "Mindre fartyg"


def main(argv: list[str]) -> None:
    names = [a for a in argv[1:] if a in AOIS] or ["hormuz", "singapore"]
    use_sam = "--no-sam" not in argv
    detections, scenes = [], []
    for name in names:
        t0 = time.time()
        dets, meta = detect_fast(name)
        print(f"[{name}] tröskling: {meta['n']} detektioner "
              f"({meta['land_masked']} landmaskade) på {time.time()-t0:.0f}s", flush=True)
        sized = []
        if use_sam:
            t1 = time.time()
            try:
                sized = size_with_sam(name)
                print(f"[{name}] SAM: {len(sized)} storleksbestämda på "
                      f"{time.time()-t1:.0f}s", flush=True)
            except Exception as exc:  # noqa: BLE001 — SAM är berikning, inte krav
                print(f"[{name}] SAM misslyckades: {exc}", flush=True)
        # fäst SAM-längder på närmaste tröskeldetektion (≤500 m)
        for s in sized:
            best, bestd = None, 0.5
            for d in dets:
                dd = math.hypot((d["lat"] - s["lat"]) * 111.0,
                                (d["lon"] - s["lon"]) * 111.0
                                * math.cos(math.radians(s["lat"])))
                if dd < bestd:
                    best, bestd = d, dd
            if best is not None:
                best["length_m"] = s["length_m"]
                best["class"] = s["class"]
            else:
                dets.append({**s, "px": 0})
        for d in dets:
            d["aoi"] = name
        detections += dets
        meta["sized"] = len(sized)
        scenes.append(meta)

    payload = {
        "detections": detections,
        "scenes": scenes,
        "sized_total": sum(1 for d in detections if d.get("length_m")),
        "note": ("Sentinel-1 GRD VV. Tröskling (15 sigma MAD) räknar skroven; "
                 "SAM (segment-geospatial, vit_b) storleksbestämmer de "
                 "tydligaste. ~1 km lägesosäkerhet (GCP-affin). Detektion "
                 "≠ identifierat fartyg — plattformar och öar kan förekomma."),
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"skrev {OUT_PATH.name}: {len(detections)} detektioner, "
          f"{payload['sized_total']} med längd", flush=True)
    os._exit(0)   # GDAL:s curl-cleanup kan hänga processen efter fjärrläsning


if __name__ == "__main__":
    main(sys.argv)
