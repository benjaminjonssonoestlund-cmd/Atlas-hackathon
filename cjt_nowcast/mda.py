"""Facit: Cargojets kvartalssiffror ur MD&A, 2016Q1–2026Q2.

Kör:  python -m cjt_nowcast.mda            (hämtar det som saknas, bygger CSV, skriver täckning)
      python -m cjt_nowcast.mda --offline  (bara cache: text + manual/)

Flöde: SOURCES → data/raw/mda/<q>.pdf (en gång) → data/processed/mda_text/<q>.txt
(pdfplumber, cachad) → parse_mda() → manual/mda_overrides.csv → data/processed/mda_quarterly.csv.
Allt efter nedladdningen går offline.

Definitioner — CAD miljoner, EPS i CAD, som ursprungligen rapporterat i kvartalets EGEN MD&A:
- core_rev = domestic network (t.o.m. 2019Q2 kallat "Core overnight") + ACMI + all-in charter.
- fuel_surcharge_other_rev: från 2023Q2 en egen rad "Fuel surcharge and other revenues".
  Tidigare = "Fuel surcharge and other pass through revenues" (inkl. FBO) + "Lease and other
  revenue"/"Other revenue(s)" — samma aggregat som den senare raden, så
  core + fuel_surcharge_other + amortization = total i alla epoker.
  2023Q1–Q3 nettar raden "Other revenue, incentives and warrant assets amortization" in
  warrant-amorteringen; amortization_contract_assets är då tom och notes säger det.
- fleet_operating: "Cargo operating fleet" när raden finns (2023Q2→), annars tabellens
  totalrad som rapporterad — den inkluderar B727/Challenger/passagerarplan (står i notes).
- direct_cost_per_block_hour = direct_expenses·1e6/block_hours, beräknad HÄR —
  Cargojet rapporterar inte måttet.
- direct_expenses_breakdown_json: varje rad i tremånaders kostnadsspecifikationen ("Fuel costs"
  … fram till "Total direct expenses"), nycklar normaliserade mellan epoker (Aircraft Cost/Costs
  → "Aircraft cost", "COS Depreciation" → "Depreciation"). T.o.m. 2021Q3: Commercial and other
  costs; 2021Q4→ uppdelad i Ground services, Airport services, Navigation and insurance.
  depreciation_in_direct = raden "Depreciation" (2021Q4–2022Q1 "COS Depreciation") där.
- sga = "Selling, general & administrative expenses" (före 2022 = Total SG&A & Marketing, inkl.
  SG&A-avskrivning); net_earnings = sammanfattningens nettoresultatrad (etiketten varierar).
- adj_earnings = "Adjusted net earnings" — i sammanfattningen från 2023Q4; 2022Q4–2023Q3 bara i
  kvartalstabellen (notes säger det); 2022Q2–Q3 rapporteras justerad EPS men inte täljaren.
- diluted_shares_m = "Average number of shares - diluted (in thousands)" / 1000. Från 2025Q4
  redovisas bara vägt antal BASIC-aktier (i miljoner) — då tom, basic-talet står i notes.
- 2016Q1 och 2017Q1 finns inte arkiverade → härledda som 6M (Q2-MD&A) − Q2. EPS går inte
  att subtrahera (antal aktier skiljer) och tas i stället ur Q2-MD&A:ns kvartalstabell;
  flottan (ögonblicksbild) ur q+4:s jämförelsekolumn.
- Omräkningar: jämförelsekolumnen i q+4:s MD&A ställs mot q; avvikelser hamnar i notes.
- py_*: q−4 som tryckt i q:s EGEN MD&A (tremånaders jämförelsekolumn, alltså omräknad där bolaget
  räknat om) — för like-for-like YoY. py_x i q är per konstruktion q+4:s omräkning av x i q−4.
  Härledda Q1: 6M-fg-år − Q2-fg-år ur Q2-MD&A:n; py_adj_eps/py_adj_earnings lämnas tomma där.
- Rader i notes som börjar med "CHECK:" är valideringsfel som inte gått att lösa.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from cjt_nowcast.common import (BROWSER_UA, MANUAL, PROCESSED, RAW, fetch_cached,
                                quarter_bounds, quarters_between, shift_quarter)

LOG = logging.getLogger("cjt.mda")

MDA_RAW = RAW / "mda"
TEXT_DIR = PROCESSED / "mda_text"
OUT_CSV = PROCESSED / "mda_quarterly.csv"
OVERRIDES = MANUAL / "mda_overrides.csv"
FIRST, LAST = "2016Q1", "2026Q2"

_WB = "https://web.archive.org/web/{ts}id_/{url}"

# 2016Q1 och 2017Q1 saknas i Wayback (CDX: bara en 302 för MDA033116, inget alls för q117)
SOURCES: dict[str, str] = {
    "2016Q2": _WB.format(ts="20240714222959", url="https://cargojet.com/financials/q216/MDA063016.pdf"),
    "2016Q3": _WB.format(ts="20240326121915", url="https://cargojet.com/financials/q316/MDA093016.pdf"),
    "2016Q4": _WB.format(ts="20240716203131", url="https://cargojet.com/financials/q416/MDA123116.pdf"),
    "2017Q2": _WB.format(ts="20240417100629", url="https://www.cargojet.com/financials/q217/MDA063017.pdf"),
    "2017Q3": _WB.format(ts="20240717224248", url="https://cargojet.com/financials/q317/MDA093017.pdf"),
    "2017Q4": _WB.format(ts="20190929230349", url="http://www.cargojet.com/financials/q417/MDA123117.pdf"),
    "2018Q1": _WB.format(ts="20210509040044", url="http://www1.cargojet.com/financials/q118/MDA033118.pdf"),
    "2018Q2": _WB.format(ts="20191104171048", url="http://www.cargojet.com:80/financials/q218/MDA063018.pdf"),
    "2018Q3": _WB.format(ts="20210509033945", url="http://www1.cargojet.com/financials/q318/MDA093018.pdf"),
    "2018Q4": _WB.format(ts="20190512130125", url="http://www.cargojet.com/financials/q418/MDA123118.pdf"),
    "2019Q1": _WB.format(ts="20210517193836", url="https://cargojet.com/financials/q119/MDA033119.pdf"),
    "2019Q2": _WB.format(ts="20191029124134", url="http://www.cargojet.com:80/financials/q219/MDA063019.pdf"),
    "2019Q3": _WB.format(ts="20210517201743", url="https://cargojet.com/financials/q319/MDA093019.pdf"),
    "2019Q4": _WB.format(ts="20200331140350", url="http://www.cargojet.com:80/financials/q419/MDA123119.pdf"),
    # 20200707-snapshotet är en brandväggs-HTML-sida; 2021-snapshotet är den riktiga PDF:en
    "2020Q1": _WB.format(ts="20210509050833", url="http://www1.cargojet.com/financials/q120/MDA033120.pdf"),
    # 2020/2021-snapshoten av Q2–Q4 2020 kommer från Common Crawl och är avklippta vid
    # 1 MiB ("wayback content truncated by length") — 2024-snapshoten är hela filer.
    "2020Q2": _WB.format(ts="20240329215306", url="https://cargojet.com/financials/q220/MDA063020.pdf"),
    "2020Q3": _WB.format(ts="20240218140555", url="https://cargojet.com/financials/q320/MDA093020.pdf"),
    "2020Q4": _WB.format(ts="20240331101847", url="https://cargojet.com/financials/q420/MDA123120.pdf"),
    "2021Q1": "https://cargojet.com/wp-content/uploads/2021/05/121CJT-Q1-2021-MDA.pdf",
    "2021Q2": "https://cargojet.com/wp-content/uploads/2021/08/221CJT-Q2-2021-MDA-2021-Final.pdf",
    "2021Q3": "https://cargojet.com/wp-content/uploads/2021/11/CJT-Q3-2021-MDA-Final.pdf",
    "2021Q4": "https://cargojet.com/wp-content/uploads/2022/03/CJT-MDA-2021-YE.pdf",
    "2022Q1": "https://cargojet.com/wp-content/uploads/2022/05/CJT-Q1-2022-MD-A-Final.pdf",
    "2022Q2": "https://cargojet.com/wp-content/uploads/2022/07/CJT-Q2-2022-MD-A-Final.pdf",
    "2022Q3": "https://cargojet.com/wp-content/uploads/2022/10/1.-CJT-Q3-2022-MD-A-Final.pdf",
    "2022Q4": "https://cargojet.com/wp-content/uploads/2023/03/cargojet_MDA.pdf",
    "2023Q1": "https://cargojet.com/wp-content/uploads/2023/04/CJT-MD-and-A-Q1-2023-Final.pdf",
    "2023Q2": "https://cargojet.com/wp-content/uploads/2023/08/Management-Discussion-Analysis-Q2-2023.pdf",
    "2023Q3": "https://cargojet.com/wp-content/uploads/2023/11/CJT-Q3-2023-MD-and-A-Final.docx.pdf",
    "2023Q4": "https://cargojet.com/wp-content/uploads/2024/02/CJT-MD-and-A-Q4-2023.pdf",
    "2024Q1": "https://cargojet.com/wp-content/uploads/2024/04/CJT-Q1-MD-and-A.pdf",
    "2024Q2": "https://cargojet.com/wp-content/uploads/2024/08/CJT-Q2-MD-and-A.pdf",
    "2024Q3": "https://cargojet.com/wp-content/uploads/2024/11/CJT-Q3-MD-and-A.pdf",
    "2024Q4": "https://cargojet.com/wp-content/uploads/2025/02/CJT-MD-and-A-Q4-2024.pdf",
    "2025Q1": "https://cargojet.com/wp-content/uploads/2025/04/Cargojet-Inc.-MDA-Q1-2025-Final.pdf",
    "2025Q2": "https://cargojet.com/wp-content/uploads/2025/08/CJT_MDA_Q2-2025-FILING.pdf",
    "2025Q3": "https://cargojet.com/wp-content/uploads/2025/11/CJT-MDandA_FINAL.pdf",
    "2025Q4": "https://cargojet.com/wp-content/uploads/2026/02/CJT-MDA_FINAL.pdf",
    "2026Q1": "https://cargojet.com/wp-content/uploads/2026/05/CJT-MDandA-Q1-2026.pdf",
    "2026Q2": "https://cargojet.com/wp-content/uploads/2026/08/CJT-MDA-Q2-2026.pdf",
}

# Q1 som härleds ur Q2-MD&A:ns sexmånaderskolumner
DERIVED_FROM_6M = {"2016Q1": "2016Q2", "2017Q1": "2017Q2"}

FLOW_FIELDS = ["block_hours", "operating_days", "domestic_rev", "acmi_rev", "charter_rev",
               "core_rev", "fuel_surcharge_other_rev", "amortization_contract_assets",
               "total_rev", "direct_expenses", "fuel_costs", "depreciation_in_direct",
               "adj_ebitda", "sga", "net_earnings", "adj_earnings"]
EPS_FIELDS = ["adj_eps", "eps_diluted"]
SHARE_FIELDS = ["diluted_shares_m"]
FLEET_FIELDS = ["fleet_operating", "fleet_b757", "fleet_b767_200", "fleet_b767_300"]
VALUE_FIELDS = FLOW_FIELDS + EPS_FIELDS + SHARE_FIELDS + FLEET_FIELDS
INT_FIELDS = {"block_hours", "operating_days", *FLEET_FIELDS}

COLUMNS = (["quarter", "period_end", "mda_date"]
           + ["block_hours", "operating_days", "domestic_rev", "acmi_rev", "charter_rev",
              "core_rev", "fuel_surcharge_other_rev", "amortization_contract_assets",
              "total_rev", "direct_expenses", "fuel_costs", "depreciation_in_direct",
              "adj_ebitda", "sga", "net_earnings", "adj_earnings", "adj_eps",
              "eps_diluted", "diluted_shares_m", "fleet_operating", "fleet_b757", "fleet_b767_200",
              "fleet_b767_300", "direct_cost_per_block_hour", "direct_expenses_breakdown_json",
              "source_url", "derived", "notes"])

# Jämförelsekolumnen (q−4) EXAKT som tryckt i q:s egen MD&A — omräknad där bolaget räknat om,
# så att YoY-tillväxt mäts på samma definition. Läggs sist för att inte flytta befintliga kolumner.
PY_FIELDS = ["block_hours", "domestic_rev", "acmi_rev", "charter_rev", "core_rev",
             "fuel_surcharge_other_rev", "amortization_contract_assets", "total_rev",
             "direct_expenses", "fuel_costs", "depreciation_in_direct", "adj_ebitda",
             "adj_eps", "adj_earnings"]
COLUMNS += [f"py_{f}" for f in PY_FIELDS]

# ---------- Hämtning och textcache ----------


def _check_pdf(path: Path) -> None:
    """HTML-felsida sparad som .pdf ska smälla — och raderas så att omkörning hämtar igen."""
    data = path.read_bytes()
    if b"%PDF" not in data[:1024]:
        path.unlink()
        raise ValueError(f"{path.name} är ingen PDF (början: {data[:60]!r}) — raderad")
    # Wayback har levererat avklippta svar (exakt 1 MiB utan %%EOF) som ändå börjar med %PDF
    if b"%%EOF" not in data[-4096:]:
        path.unlink()
        raise ValueError(f"{path.name} saknar %%EOF ({len(data)} byte, trunkerad?) — raderad")


def fetch_all(refresh: bool = False) -> dict[str, Path]:
    """Ladda ner alla MD&A en gång. Fel samlas och kastas i slutet, efter att resten hämtats."""
    out, errors = {}, {}
    for q, url in SOURCES.items():
        wayback = "web.archive.org" in url
        dest = MDA_RAW / f"{q}.pdf"
        cached = dest.exists() and not refresh
        try:
            p = fetch_cached(url, dest, headers={"User-Agent": BROWSER_UA},
                             min_interval=8.0 if wayback else 1.0, refresh=refresh)
            _check_pdf(p)
        except Exception as exc:  # noqa: BLE001 — samla, smäll i slutet
            errors[q] = exc
            LOG.error("%s misslyckades: %s", q, exc)
            continue
        out[q] = p
        LOG.debug("%s %s", q, "cache" if cached else "hämtad")
    if errors:
        raise RuntimeError(f"MD&A-hämtning misslyckades för {sorted(errors)}: {errors}")
    return out


def extract_text(q: str, refresh: bool = False) -> str:
    """PDF → text, cachad. Sidmarkörer behålls så att överstyrningar kan citera sida."""
    dest = TEXT_DIR / f"{q}.txt"
    if dest.exists() and not refresh:
        return dest.read_text(encoding="utf-8")
    import pdfplumber  # tung import — bara när cachen saknas

    parts = []
    with pdfplumber.open(MDA_RAW / f"{q}.pdf") as pdf:
        for i, page in enumerate(pdf.pages, 1):
            parts.append(f"<<<PAGE {i}>>>\n{page.extract_text() or ''}")
    text = "\n".join(parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return text


# ---------- Radparser ----------

_NUM_TOKEN = re.compile(r"^\$?\(?\$?-?[\d,]*\.?\d+%?\)?%?$")
_NIL = {"-", "–", "—", "$-"}
_NA = {"nm", "n/a", "n.m."}
_FOOTNOTE = re.compile(r"^\(\d{1,2}\)$")


def _is_num(tok: str) -> bool:
    return tok == "$" or tok in _NIL or tok.lower() in _NA or bool(_NUM_TOKEN.match(tok))


def _num(tok: str) -> float | None:
    """'($1.3)' → -1.3, '15,787' → 15787, '-' → 0.0 (tabellens noll), 'nm' → None."""
    if tok in _NIL:
        return 0.0
    if tok.lower() in _NA:
        return None
    neg = "(" in tok or tok.lstrip("$(").startswith("-")
    v = float(re.sub(r"[^\d.]", "", tok))
    return -v if neg else v


def _norm(label: str) -> str:
    """Etikett → nyckel. pdfplumber bryter ord ('Tota l revenues') och fotnoter varierar."""
    label = re.sub(r"\(\s*\d{1,2}\s*\)|\([A-Z]\)", "", label)
    return re.sub(r"[^a-z0-9]", "", label.lower())


@dataclass
class Row:
    key: str
    label: str
    vals: list
    line: int
    page: int


def parse_rows(text: str) -> list[Row]:
    """Tabellrader: etikett följd av minst ett tal. Siffrorna läses bakifrån."""
    rows: list[Row] = []
    page, prev_text = 0, None
    for i, raw in enumerate(text.splitlines()):
        m = re.match(r"<<<PAGE (\d+)>>>", raw)
        if m:
            page, prev_text = int(m.group(1)), None
            continue
        # modellnumret i typnamnet ("Challenger 601", "Cessna 750") ska inte läsas som ett värde
        toks = re.sub(r"\b(Challenger|Cessna|ATR|Dash) (\d{2,3})\b", r"\1-\2", raw).split()
        num_toks: list[str] = []
        while toks and _is_num(toks[-1]):
            num_toks.append(toks.pop())
        num_toks = [t for t in reversed(num_toks) if t != "$"]
        label = " ".join(toks)
        # fotnotsmarkör direkt efter etiketten: "Block hours (5) 15,787 …"
        while label and len(num_toks) >= 2 and _FOOTNOTE.match(num_toks[0]):
            num_toks.pop(0)
        # ett ensamt avslutande "-" är ett bindestreck i en bruten etikett
        # ("Average number of shares -" / "diluted 10,640 …"), inte ett nollvärde
        if not num_toks or (label and len(num_toks) == 1 and num_toks[0] in _NIL):
            prev_text = raw.strip() or None
            continue
        # etikett bruten över två rader: "Domestic network, ACMI and charter" / "revenues $219.9 …"
        if prev_text and label[:1].islower():
            label = f"{prev_text} {label}"
        nil_second = len(num_toks) > 1 and num_toks[1] in _NIL
        vals = [_num(t) for t in num_toks]
        # lösryckt "-" mellan kolumnerna ("61.6 - 78.2 (16.6) -21.2%"): släng den om
        # förändringskolumnen då går ihop, annars blir föregående år felaktigt 0
        if (nil_second and len(vals) >= 4 and None not in vals[:4]
                and abs(vals[0] - vals[1] - vals[2]) > 0.15
                and abs(vals[0] - vals[2] - vals[3]) <= 0.15):
            vals.pop(1)
        rows.append(Row(_norm(label), label, vals, i, page))
        prev_text = None
    return rows


# ---------- Dokumentparser ----------

K_DOMESTIC = {"coreovernightrevenues", "domesticnetworkrevenues"}
K_CORE = {"totalovernightacmiandcharterrevenues", "totaldomesticnetworkacmiandcharterrevenues",
          "totaldomesticnetworkacmiandcharter"}
K_FSO = {"fuelsurchargeandotherrevenues", "totalfuelsurchargeandotherrevenues"}
K_PASSTHROUGH = {"fuelsurchargeandotherpassthroughrevenues"}
K_OTHER = {"leaseandotherrevenue", "otherrevenue", "otherrevenues"}
K_OTHER_NETTED = {"otherrevenueincentivesandwarrantassetsamortization"}
K_AMORT = {"amortizationofstockwarrantcontractassets", "amortizationofcontractassets"}
K_SGA = {"sellinggeneraladministrativeexpenses", "sellinggeneralandadministrativeexpenses"}
K_ADJ_EARN = {"adjustednetearnings", "adjustednetlossearnings", "adjustednetearningsloss"}

# Kostnadsraderna byter versaler/plural mellan epoker — samma nyckel i JSON:en
_DIRECT_CANON = {
    "fuelcosts": "Fuel costs", "depreciation": "Depreciation",
    "cosdepreciation": "Depreciation",  # 2021Q4–2022Q1 heter raden "COS Depreciation"
    "aircraftcost": "Aircraft cost", "aircraftcosts": "Aircraft cost",
    "heavymaintenanceamortization": "Heavy maintenance amortization",
    "maintenancecost": "Maintenance cost", "maintenancecosts": "Maintenance cost",
    "crewcosts": "Crew costs", "commercialandothercosts": "Commercial and other costs",
    "groundservices": "Ground services", "airportservices": "Airport services",
    "navigationandinsurance": "Navigation and insurance",
}

_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
_DATE_PATTERNS = [
    rf"effective date of (?:the|this) MD&A is ({_MONTH} \d{{1,2}}, ?\d{{4}})",
    rf"MD&A is dated (?:as of )?({_MONTH} \d{{1,2}}, ?\d{{4}})",
    # 2020Q3 skriver "The MD&A were approved …"
    rf"MD&A (?:was|were) approved by the Board of Directors.{{0,80}}?authorized for issuance on ({_MONTH} \d{{1,2}}, ?\d{{4}})",
]


def _first(rows: list[Row], keys: set[str], min_vals: int = 2) -> Row | None:
    for r in rows:
        if r.key in keys and len(r.vals) >= min_vals:
            return r
    return None


def _at(r: Row | None, col: int | None) -> float | None:
    if r is None or col is None or len(r.vals) <= col:
        return None
    # YTD-kolumnerna (index 4–5) finns bara på rader med full åttakolumnsbredd
    if col >= 4 and len(r.vals) < 8:
        return None
    return r.vals[col]


def _mda_date(text: str) -> str | None:
    flat = " ".join(text.split())
    for pat in _DATE_PATTERNS:
        m = re.search(pat, flat)
        if m:
            s = re.sub(r",\s*", ", ", m.group(1))
            return datetime.strptime(s, "%B %d, %Y").date().isoformat()
    return None


def mentions_period_end(text: str, q: str) -> bool:
    """Titelsidan ska nämna periodslutet — skyddar mot fel fil bakom en URL."""
    end = quarter_bounds(q)[1]
    return f"{end:%B} {end.day}, {end.year}" in " ".join(text[:4000].split())


def _summary_rows(rows: list[Row], lines: list[str]) -> list[Row]:
    start = None
    for i, line in enumerate(lines):
        n = _norm(line)
        # innehållsförteckningens rad slutar på sidnummer, rubriken på "highlights"
        if "operatingstatistics" in n and n.endswith("highlights"):
            start = i
            break
    if start is None:
        raise ValueError("hittar ingen 'Operating Statistics Highlights'-tabell")
    out = []
    for r in rows:
        if r.line <= start:
            continue
        if r.line > start + 120:
            break
        out.append(r)
        if "headcount" in r.key or r.key.endswith("employees"):
            break
    return out


def _breakdown_blocks(rows: list[Row], after_line: int) -> list[list[Row]]:
    """Intäktstabellerna: först tremånaders (Q4: kvartalet), sedan YTD/helår."""
    heads = [r for r in rows if r.line > after_line and r.key in K_DOMESTIC and len(r.vals) >= 3]
    blocks = []
    for k, h in enumerate(heads[:2]):
        stop = heads[k + 1].line if k + 1 < len(heads) else h.line + 250
        blocks.append([r for r in rows if h.line <= r.line < min(stop, h.line + 250)])
    return blocks


def _adjusted_eps_row(summ: list[Row], rows: list[Row]) -> Row | None:
    """Justerad EPS heter bara "Adjusted" och står direkt under "Diluted"."""
    for pool, widths in ((summ, None), (rows, (2, 4))):
        for a, b in zip(pool, pool[1:]):
            if a.key == "diluted" and b.key == "adjusted" and (widths is None or len(b.vals) in widths):
                return b
    return None


def _fleet_total(summ: list[Row]) -> Row | None:
    r = _first(summ, {"cargooperatingfleet"})
    if r is not None:
        return r
    for i, r in enumerate(summ):
        if r.key == "b767300":
            for nxt in summ[i + 1:i + 4]:
                if nxt.key == "":
                    return nxt
    return None


def _direct_rows(block: list[Row] | None) -> list[Row]:
    """Kostnadsspecifikationen: från "Fuel costs" fram till (exkl.) "Total direct expenses"."""
    block = block or []
    start = next((i for i, r in enumerate(block) if r.key == "fuelcosts" and len(r.vals) >= 3), None)
    if start is None:
        return []
    end = next((i for i in range(start, len(block)) if block[i].key == "totaldirectexpenses"), None)
    if end is None:
        return []
    return [r for r in block[start:end] if len(r.vals) >= 3]


def _canon_direct(r: Row) -> str:
    return _DIRECT_CANON.get(r.key) or " ".join(re.sub(r"\(\s*\d{1,2}\s*\)", "", r.label).split())


def _net_earnings_row(summ: list[Row]) -> Row | None:
    # "Net Income (loss)", "Net (loss) earning", "Net earnings (loss)" … men inte "Net finance costs"
    for r in summ:
        k = r.key
        if k.startswith("net") and ("earning" in k or "income" in k) and "finance" not in k and len(r.vals) >= 2:
            return r
    return None


def _shares_m(r: Row | None, col: int) -> float | None:
    """Aktieantal i miljoner; tabellerna anger tusental utom där etiketten säger millions."""
    v = _at(r, col)
    if v is None:
        return None
    return round(v if "millions" in r.key else v / 1000.0, 3)


def _share_row(rows: list[Row], kind: str, widths: tuple[int, ...]) -> Row | None:
    return next((r for r in rows if "numberofshares" in r.key and kind in r.key
                 and len(r.vals) in widths), None)


def _extract(rows, summ, block, cs, cb, conflicts=None) -> dict:
    """Plocka fälten ur sammanfattningen (kolumn cs) och intäktstabellen (kolumn cb)."""
    s = lambda keys: _at(_first(summ, keys), cs)  # noqa: E731
    b = lambda keys: _at(_first(block or [], keys), cb)  # noqa: E731
    direct = _direct_rows(block)
    out = {
        "block_hours": s({"blockhours"}),
        "operating_days": s({"operatingdays"}),
        "adj_ebitda": s({"adjustedebitda"}),
        "sga": s(K_SGA),
        "net_earnings": _at(_net_earnings_row(summ), cs),
        "domestic_rev": b(K_DOMESTIC),
        "acmi_rev": b({"acmirevenues"}),
        "charter_rev": b({"allincharterrevenues"}),
        "fuel_costs": b({"fuelcosts"}),
        "depreciation_in_direct": _at(_first(direct, {"depreciation", "cosdepreciation"}), cb),
        "_direct_breakdown": {_canon_direct(r): _at(r, cb) for r in direct},
    }
    if cs is not None and cs < 4:
        out["eps_diluted"] = s({"diluted"})
        out["adj_eps"] = _at(_adjusted_eps_row(summ, rows), cs)
        # sammanfattningen först, sedan avstämningstabellen (2 eller 4 kolumner: 3M nu/fg år, YTD)
        adj = _first(summ, K_ADJ_EARN) or next(
            (r for r in rows if r.key in K_ADJ_EARN and len(r.vals) in (2, 4)), None)
        out["adj_earnings"] = _at(adj, cs)
        out["diluted_shares_m"] = _shares_m(_share_row(rows, "diluted", (2, 3, 4)), cs)
        out["fleet_b757"] = s({"b757200"})
        out["fleet_b767_200"] = s({"b767200"})
        out["fleet_b767_300"] = s({"b767300"})
        out["fleet_operating"] = _at(_fleet_total(summ), cs)

    s_total = s({"totalrevenues"})
    if s_total is None:
        s_total = s({"revenues"})
    s_core, s_fso, s_amort, s_dir = (s({"domesticnetworkacmiandcharterrevenues"}), s(K_FSO),
                                     s(K_AMORT), s({"directexpenses"}))
    b_total, b_core, b_amort, b_dir = b({"totalrevenues"}), b(K_CORE), b(K_AMORT), b({"totaldirectexpenses"})
    b_fso = b(K_FSO)
    if b_fso is None:
        pt, oth = b(K_PASSTHROUGH), b(K_OTHER | K_OTHER_NETTED)
        if pt is not None and oth is not None:
            b_fso = round(pt + oth, 1)
    pick = lambda sv, bv: sv if sv is not None else bv  # noqa: E731
    out.update(total_rev=pick(s_total, b_total), core_rev=pick(s_core, b_core),
               fuel_surcharge_other_rev=pick(s_fso, b_fso),
               amortization_contract_assets=pick(s_amort, b_amort),
               direct_expenses=pick(s_dir, b_dir))
    if conflicts is not None:
        for f, sv, bv in (("total_rev", s_total, b_total), ("core_rev", s_core, b_core),
                          ("fuel_surcharge_other_rev", s_fso, b_fso),
                          ("amortization_contract_assets", s_amort, b_amort),
                          ("direct_expenses", s_dir, b_dir)):
            if sv is not None and bv is not None and abs(sv - bv) > 0.051:
                conflicts.append(f"CHECK: {f} summary {sv:g} vs breakdown {bv:g}")
    return out


def parse_mda(text: str) -> dict:
    """En MD&A → {'cur', 'prior', 'ytd'} (fält → värde) + datum, anteckningar."""
    rows = parse_rows(text)
    summ = _summary_rows(rows, text.splitlines())
    blocks = _breakdown_blocks(rows, summ[-1].line if summ else 0)
    b3 = blocks[0] if blocks else None
    bytd = blocks[1] if len(blocks) > 1 else None
    notes: list[str] = []
    cur = _extract(rows, summ, b3, 0, 0, conflicts=notes)
    prior = _extract(rows, summ, b3, 1, 1)
    ytd = _extract(rows, summ, bytd, 4, 0)
    ytd_prior = _extract(rows, summ, bytd, 5, 1)  # för härledda Q1: py = 6M fg år − Q2 fg år
    breakdown, ytd_breakdown = cur.pop("_direct_breakdown"), ytd.pop("_direct_breakdown")
    prior.pop("_direct_breakdown")
    ytd_prior.pop("_direct_breakdown")
    summ_end = summ[-1].line if summ else 0

    if cur.get("adj_earnings") is None:
        # 2022Q4–2023Q3: täljaren finns bara i åttakvartalstabellen (kolumn 0 = kvartalet)
        q8 = next((r for r in rows if r.key in K_ADJ_EARN and len(r.vals) == 8 and r.line > summ_end), None)
        if q8 is not None:
            cur["adj_earnings"] = q8.vals[0]
            notes.append("adj_earnings from quarterly results table")
            if prior.get("adj_earnings") is None:
                # kolumn 4 = samma kvartal föregående år, som tryckt i denna MD&A
                prior["adj_earnings"] = q8.vals[4]
                notes.append("py_adj_earnings from quarterly results table")
    if cur.get("diluted_shares_m") is None:
        basic = _share_row(rows, "basic", (2, 3, 4))
        if basic is not None:
            notes.append(f"diluted shares not reported; basic weighted avg {_shares_m(basic, 0):g}M")

    if _first(b3 or [], K_OTHER_NETTED) is not None and cur["amortization_contract_assets"] is None:
        notes.append("fuel_surcharge_other_rev nets warrant amortization "
                     "('Other revenue, incentives and warrant assets amortization'); amortization not separated")
    if cur.get("fleet_operating") is not None and _first(summ, {"cargooperatingfleet"}) is None:
        # allt i flottblocket utöver B757/B767 (B727, Challenger, Cessna 750, passagerarplan …)
        keys = [r.key for r in summ]
        lo = min((i for i, k in enumerate(keys) if k.startswith(("b727", "b757", "b767"))), default=None)
        hi = next((i for i, k in enumerate(keys) if k == "" and lo is not None and i > lo), None)
        fleet_block = summ[lo:hi] if lo is not None and hi is not None else []
        extra = [f"{r.label}={r.vals[0]:g}" for r in fleet_block
                 if r.key not in {"b757200", "b767200", "b767300"} and r.vals[0]]
        if extra:
            notes.append("fleet_operating is reported total incl. " + ", ".join(extra))

    # EPS per kvartal ur åttakvartalstabellen ("Summary of … Quarterly Results"). Sammanfattningens
    # Diluted-rad har också åtta tal (3M+YTD med förändring) — därför bara rader efter den.
    qtab = [r for r in rows if r.key == "diluted" and len(r.vals) == 8 and r.line > summ_end]
    qshares = next((r for r in rows if "numberofshares" in r.key and "diluted" in r.key
                    and len(r.vals) == 8 and r.line > summ_end), None)
    return {"cur": cur, "prior": prior, "ytd": ytd, "ytd_prior": ytd_prior, "mda_date": _mda_date(text),
            "direct_breakdown": breakdown, "ytd_direct_breakdown": ytd_breakdown,
            "prev_q_eps": qtab[0].vals[1] if qtab else None,
            "prev_q_diluted_shares_m": _shares_m(qshares, 1),
            "qtab_cur_eps": qtab[0].vals[0] if qtab else None, "notes": notes}


# ---------- Poster ----------


def _round(field: str, v):
    if v is None:
        return None
    if field in INT_FIELDS:
        return int(round(v))
    if field in SHARE_FIELDS:
        return round(v, 3)
    return round(v, 2 if field in EPS_FIELDS else 1)


def _fmt(field: str, v) -> str:
    return f"{v:,}" if field in INT_FIELDS else f"{v:g}"


def _blank(q: str) -> dict:
    rec = {c: None for c in COLUMNS}
    rec.update(quarter=q, period_end=quarter_bounds(q)[1].isoformat(), derived="", _notes=[])
    return rec


def record_from_mda(q: str, parsed: dict) -> dict:
    rec = _blank(q)
    rec.update(mda_date=parsed["mda_date"], source_url=SOURCES.get(q))
    for f in VALUE_FIELDS:
        rec[f] = _round(f, parsed["cur"].get(f))
    for f in PY_FIELDS:
        rec[f"py_{f}"] = _round(f, parsed["prior"].get(f))
    if parsed.get("direct_breakdown"):
        rec["direct_expenses_breakdown_json"] = json.dumps(parsed["direct_breakdown"])
    rec["_notes"].extend(parsed["notes"])
    return rec


def derive_from_6m(q: str, src_q: str, p_src: dict, p_next: dict | None = None) -> dict:
    """Q1 = 6M − Q2 för flöden. EPS och flotta är inte additiva och hämtas annorstädes."""
    rec = _blank(q)
    rec["source_url"] = SOURCES.get(src_q)
    for f in FLOW_FIELDS:
        a, b = p_src["ytd"].get(f), p_src["cur"].get(f)
        if a is not None and b is not None:
            rec[f] = _round(f, a - b)
    how = [f"6M−Q2 ({src_q} MD&A)"]
    # jämförelsetal: 6M-föregående-år − Q2-föregående-år ur samma MD&A. EPS/justerat resultat
    # är inte additiva (aktieantal) och lämnas tomma.
    ytd_p, q2_p = p_src.get("ytd_prior") or {}, p_src.get("prior") or {}
    for f in PY_FIELDS:
        if f in FLOW_FIELDS and f != "adj_earnings":
            a, b = ytd_p.get(f), q2_p.get(f)
            if a is not None and b is not None:
                rec[f"py_{f}"] = _round(f, a - b)
    if any(rec[f"py_{f}"] is not None for f in PY_FIELDS):
        how.append(f"py_* = 6M prior-year − Q2 prior-year ({src_q} MD&A)")
    bd, ybd = p_src.get("direct_breakdown") or {}, p_src.get("ytd_direct_breakdown") or {}
    if bd and set(bd) == set(ybd):
        rec["direct_expenses_breakdown_json"] = json.dumps({k: round(ybd[k] - bd[k], 1) for k in bd})
    if p_src.get("prev_q_eps") is not None:
        rec["eps_diluted"] = p_src["prev_q_eps"]
        how.append(f"eps_diluted from {src_q} quarterly results table")
    if p_src.get("prev_q_diluted_shares_m") is not None:
        rec["diluted_shares_m"] = p_src["prev_q_diluted_shares_m"]
        how.append(f"diluted_shares_m from {src_q} quarterly results table")
    if p_next is not None:
        for f in FLEET_FIELDS:
            rec[f] = _round(f, p_next["prior"].get(f))
        if any(rec[f] is not None for f in FLEET_FIELDS):
            how.append(f"fleet from {shift_quarter(q, 4)} prior-year column")
    rec["derived"] = "; ".join(how)
    rec["_notes"].append("no MD&A archived for this quarter; mda_date unknown")
    return rec


def restatement_notes(q: str, rec: dict, parsed: dict[str, dict]) -> list[str]:
    """Jämförelsekolumnen i q+4 ska vara lika med q; allt annat är en omräkning."""
    q4 = shift_quarter(q, 4)
    if q4 not in parsed:
        return []
    out = []
    word = "derived" if rec["derived"] else "orig"
    for f in VALUE_FIELDS:
        new, old = _round(f, parsed[q4]["prior"].get(f)), rec.get(f)
        if new is None:
            continue
        if old is None:
            out.append(f"{q4} prior-year col reports {f} {_fmt(f, new)} (not in {word})")
            continue
        tol = (0 if f in INT_FIELDS else 0.0005 if f in SHARE_FIELDS
               else 0.005 if f in EPS_FIELDS else 0.05)
        if abs(new - old) > tol + 1e-9:
            out.append(f"restated in {q4}: {f} {_fmt(f, new)} vs {word} {_fmt(f, old)}")
    return out


def row_checks(rec: dict, prefix: str = "") -> list[str]:
    """Summakontroller per kvartal (±0.15 MCAD). prefix="py_" kontrollerar jämförelsekolumnen."""
    out = []

    def g(f):
        v = rec.get(prefix + f)
        return None if v is None or (isinstance(v, float) and pd.isna(v)) else v

    parts = [g("domestic_rev"), g("acmi_rev"), g("charter_rev")]
    if None not in parts and g("core_rev") is not None:
        s = sum(parts)
        if abs(s - g("core_rev")) > 0.15:
            out.append(f"{prefix}domestic+acmi+charter {s:.1f} != {prefix}core_rev {g('core_rev'):g}")
    if None not in (g("core_rev"), g("fuel_surcharge_other_rev"), g("total_rev")):
        s = g("core_rev") + g("fuel_surcharge_other_rev") + (g("amortization_contract_assets") or 0.0)
        if abs(s - g("total_rev")) > 0.15:
            out.append(f"{prefix}core+fuel_surcharge_other+amortization {s:.1f} != "
                       f"{prefix}total_rev {g('total_rev'):g}")
    bd = None if prefix else g("direct_expenses_breakdown_json")
    if bd and g("direct_expenses") is not None:
        items = [v for v in json.loads(bd).values() if v is not None]
        # varje rad är avrundad till 0.1 → summan kan glida n·0.05
        if abs(sum(items) - g("direct_expenses")) > 0.05 * len(items) + 0.05:
            out.append(f"direct breakdown sums to {sum(items):.1f} != direct_expenses {g('direct_expenses'):g}")
    return out


def load_overrides(path: Path = OVERRIDES) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("quarter") or "").strip()
                and not r["quarter"].startswith("#")]


def _apply_override(rec: dict, o: dict) -> None:
    f = o["field"].strip()
    if f == "mda_date":
        rec[f] = o["value"].strip()
    elif f in VALUE_FIELDS:
        rec[f] = _round(f, float(o["value"]))
    else:
        raise ValueError(f"okänt fält i mda_overrides: {f}")
    note = f": {o['note']}" if o.get("note") else ""
    rec["_notes"].append(f"override {f}={o['value']} (p.{o.get('page', '?')}{note})")


# ---------- Bygg ----------


def build(write: bool = True) -> pd.DataFrame:
    """Bygg facit ur textcachen (+ PDF-cachen om text saknas). Inga nätverksanrop."""
    texts = {q: extract_text(q) for q in SOURCES}
    parsed = {q: parse_mda(t) for q, t in texts.items()}

    records: dict[str, dict] = {}
    for q in quarters_between(FIRST, LAST):
        if q in parsed:
            rec = record_from_mda(q, parsed[q])
            if not mentions_period_end(texts[q], q):
                rec["_notes"].append("CHECK: title page does not mention period end")
            qt = parsed[q]["qtab_cur_eps"]
            if qt is not None and rec["eps_diluted"] is not None and abs(qt - rec["eps_diluted"]) > 0.005:
                rec["_notes"].append(f"CHECK: eps_diluted {rec['eps_diluted']} vs quarterly table {qt}")
        elif q in DERIVED_FROM_6M:
            src = DERIVED_FROM_6M[q]
            rec = derive_from_6m(q, src, parsed[src], parsed.get(shift_quarter(q, 4)))
        else:
            rec = _blank(q)
            rec["_notes"].append("CHECK: no source")
        records[q] = rec

    for q, rec in records.items():
        rec["_notes"].extend(restatement_notes(q, rec, parsed))
    for o in load_overrides():
        _apply_override(records[o["quarter"].strip()], o)
    for rec in records.values():
        if rec["direct_expenses"] is not None and rec["block_hours"]:
            rec["direct_cost_per_block_hour"] = int(round(rec["direct_expenses"] * 1e6 / rec["block_hours"]))
        rec["_notes"].extend(f"CHECK: {m}" for m in row_checks(rec) + row_checks(rec, "py_"))
        rec["notes"] = "; ".join(rec.pop("_notes"))

    df = pd.DataFrame(list(records.values()), columns=COLUMNS)
    for c in [*INT_FIELDS, "direct_cost_per_block_hour"]:
        df[c] = pd.array([None if pd.isna(v) else int(v) for v in df[c]], dtype="Int64")
    if write:
        OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(OUT_CSV, index=False)
        LOG.info("skrev %s (%d rader)", OUT_CSV, len(df))
    return df


# ---------- Rapport ----------

_SHORT = {"mda_date": "date", "block_hours": "bh", "operating_days": "od", "domestic_rev": "dom",
          "acmi_rev": "acmi", "charter_rev": "chr", "core_rev": "core",
          "fuel_surcharge_other_rev": "fso", "amortization_contract_assets": "amo",
          "total_rev": "tot", "direct_expenses": "dir", "fuel_costs": "fuel",
          "depreciation_in_direct": "depr", "direct_expenses_breakdown_json": "dxj",
          "adj_ebitda": "ebd", "sga": "sga", "net_earnings": "net", "adj_earnings": "aern",
          "adj_eps": "aeps", "eps_diluted": "eps", "diluted_shares_m": "shr",
          "fleet_operating": "flt", "fleet_b757": "757", "fleet_b767_200": "7672",
          "fleet_b767_300": "7673"}


def _filled(v) -> bool:
    return not (v is None or v is pd.NA or (isinstance(v, float) and pd.isna(v)) or v == "")


def coverage_table(df: pd.DataFrame) -> str:
    lines = ["quarter " + " ".join(f"{s:>4}" for s in _SHORT.values()) + "  derived"]
    for _, r in df.iterrows():
        cells = " ".join(f"{'x' if _filled(r[k]) else '.':>4}" for k in _SHORT)
        lines.append(f"{r['quarter']:<7} {cells}  {r['derived'] if _filled(r['derived']) else ''}")
    return "\n".join(lines)


def validate(df: pd.DataFrame) -> list[str]:
    msgs = []
    for _, r in df.iterrows():
        q = r["quarter"]
        for part in str(r["notes"] or "").split("; "):
            if part.startswith("CHECK:"):
                msgs.append(f"{q}: {part}")
        bh = r["block_hours"]
        if _filled(bh) and not 5000 <= bh <= 20000:
            msgs.append(f"{q}: block_hours {bh:,} utanför 5k–20k (kontrollera)")
    return msgs


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--offline" not in argv:
        fetch_all(refresh="--refresh" in argv)
    df = build()
    print(coverage_table(df))
    msgs = validate(df)
    print(f"\nvalidering: {len(msgs)} anmärkningar")
    for m in msgs:
        print("  " + m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
