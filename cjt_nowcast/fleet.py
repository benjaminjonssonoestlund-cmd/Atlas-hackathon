"""Cargojets flotta: registrering → Mode S-hex + aktivt fönster.

Primärkälla: Transport Canadas civila luftfartygsregister (CCAR, öppen
nedladdning). Registret är en ögonblicksbild — plan som sålts ut ur landet
före nedladdningsdagen syns inte längre med Cargojet som ägare. De fylls i
via manual/fleet_extra.csv (med källa per rad, t.ex. arkiverad Planespotters-
sida). Kanadensisk hex är en ren funktion av registreringen (se
`hex_from_registration`), så en registrering räcker för att hämta spår.

Ögonblicksbilden sparas daterad i raw/ så att en senare körning bygger på
exakt samma register om man inte uttryckligen hämtar om.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

from .common import MANUAL, PROCESSED, RAW, fetch_cached

CCAR_URL = "https://wwwapps.tc.gc.ca/Saf-Sec-Sur/2/CCARCS-RIACC/download/ccarcsdb.zip"
OWNER_MATCH = "cargojet"
FLEET_CSV = PROCESSED / "fleet.csv"
EXTRA_CSV = MANUAL / "fleet_extra.csv"
EXTRA_COLUMNS = ["registration", "type", "msn", "active_from", "active_to", "source", "note"]

# Index i carscurr.txt / carsownr.txt (se carslayout.txt i zip-filen)
CUR_MARK, CUR_MAKE, CUR_MODEL, CUR_MSN = 0, 3, 4, 5
CUR_EFFECTIVE, CUR_INEFFECTIVE, CUR_STATUS, CUR_MODE_S = 22, 23, 38, 42
OWN_MARK, OWN_NAME, OWN_TRADE = 0, 1, 2

# Mätperiodens start — plan som lämnade registret före detta är ointressanta.
STUDY_START = date(2024, 1, 1)


def hex_from_registration(reg: str) -> str:
    """C-FAAA → c00001, C-GAAA → c044a9 (Kanadas sekventiella ICAO-block).

    Verifierat mot registrets binära Mode S-fält: C-FACJ=c0003e,
    C-FCJF=c00638, C-GCJT=c04aee.
    """
    reg = reg.upper().replace("-", "")
    if len(reg) != 5 or reg[0] != "C" or reg[1] not in "FG":
        raise ValueError(f"inte en kanadensisk C-F/C-G-registrering: {reg}")
    idx = sum((ord(ch) - 65) * 26 ** (2 - i) for i, ch in enumerate(reg[2:]))
    base = 1 if reg[1] == "F" else 1 + 26 ** 3
    return format(0xC00000 + base + idx, "06x")


def _rows(raw: bytes):
    # Registret innehåller NUL-byte och latin-1; csv-modulen tål inget av det
    text = raw.decode("latin-1").replace("\0", "")
    return csv.reader(io.StringIO(text, newline=""))


def _parse_date(s: str) -> date | None:
    s = s.strip()
    return date.fromisoformat(s.replace("/", "-")) if s else None


def _model_to_family(make: str, model: str) -> str:
    m = model.upper()
    if m.startswith("757-2"):
        return "B757-200"
    if m.startswith("767-2"):
        return "B767-200"
    if m.startswith("767-3"):
        return "B767-300"
    if m.startswith("777"):
        return "B777"
    return f"{make} {model}".strip()


def from_registry(snapshot: str | None = None, refresh: bool = False) -> pd.DataFrame:
    """Cargojet-ägda luftfartyg ur CCAR. snapshot=YYYYMMDD väljer en cachad version."""
    snap_dir = RAW / "tc_ccar"
    if snapshot is None and not refresh:
        cached = sorted(snap_dir.glob("ccarcsdb_*.zip"))
        snapshot = cached[-1].stem.split("_")[1] if cached else None
    snapshot = snapshot or date.today().strftime("%Y%m%d")
    path = fetch_cached(CCAR_URL, snap_dir / f"ccarcsdb_{snapshot}.zip", refresh=refresh)

    with zipfile.ZipFile(path) as z:
        owners = {r[OWN_MARK].strip() for r in _rows(z.read("carsownr.txt"))
                  if len(r) > OWN_TRADE
                  and OWNER_MATCH in (r[OWN_NAME] + r[OWN_TRADE]).lower()}
        out = []
        for r in _rows(z.read("carscurr.txt")):
            if len(r) <= CUR_MODE_S or r[CUR_MARK].strip() not in owners:
                continue
            reg = "C-" + r[CUR_MARK].strip()
            bits = r[CUR_MODE_S].strip()
            hx = format(int(bits, 2), "06x") if bits and set(bits) <= {"0", "1"} else ""
            if hx and hx != hex_from_registration(reg):
                raise ValueError(f"{reg}: registrets hex {hx} ≠ härledd — algoritmen stämmer inte")
            out.append({
                "registration": reg,
                "hex": hx or hex_from_registration(reg),
                "type": _model_to_family(r[CUR_MAKE].strip(), r[CUR_MODEL].strip()),
                "model": r[CUR_MODEL].strip(),
                "msn": r[CUR_MSN].strip(),
                "active_from": _parse_date(r[CUR_EFFECTIVE]),
                "active_to": _parse_date(r[CUR_INEFFECTIVE]),
                "source": f"TC CCAR {snapshot}",
                "note": r[CUR_STATUS].strip(),
            })
    return pd.DataFrame(out)


def load_extra() -> pd.DataFrame:
    if not EXTRA_CSV.exists():
        EXTRA_CSV.parent.mkdir(parents=True, exist_ok=True)
        EXTRA_CSV.write_text(",".join(EXTRA_COLUMNS) + "\n", encoding="utf-8")
    df = pd.read_csv(EXTRA_CSV, dtype=str, keep_default_na=False)
    if df.empty:
        return df
    df["hex"] = df["registration"].map(hex_from_registration)
    for col in ("active_from", "active_to"):
        df[col] = df[col].map(lambda s: _parse_date(s) if s else None)
    return df


def build(refresh: bool = False) -> pd.DataFrame:
    reg = from_registry(refresh=refresh)
    extra = load_extra()
    fleet = pd.concat([reg, extra[extra["registration"].map(
        lambda r: r not in set(reg["registration"]))]], ignore_index=True)
    # Plan vars registrering upphörde före mätperioden är irrelevanta (t.ex. 727:orna)
    keep = fleet["active_to"].map(lambda d: d is None or pd.isna(d) or d >= STUDY_START)
    fleet = fleet[keep].sort_values(["type", "registration"]).reset_index(drop=True)
    FLEET_CSV.parent.mkdir(parents=True, exist_ok=True)
    fleet.to_csv(FLEET_CSV, index=False)
    return fleet


def load() -> pd.DataFrame:
    if not FLEET_CSV.exists():
        return build()
    df = pd.read_csv(FLEET_CSV, dtype=str, keep_default_na=False)
    for col in ("active_from", "active_to"):
        df[col] = df[col].map(lambda s: _parse_date(s) if s else None)
    return df


if __name__ == "__main__":
    f = build()
    print(f.groupby("type").size().to_string())
    print(f"{len(f)} flygplan → {FLEET_CSV}")
