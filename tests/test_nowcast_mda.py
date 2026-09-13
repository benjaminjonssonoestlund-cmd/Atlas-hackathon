"""MD&A-parsern på textsnuttar ur två etikettepoker (2016 och 2026) — inga nätverksanrop.

Snuttarna är avskrifter av pdfplumber-text ur Q2 2016- och Q2 2026-MD&A:erna, inklusive
pdfplumbers egenheter (ordbrott, fotnoter, etiketter brutna över två rader).
"""

from __future__ import annotations

import pytest

from cjt_nowcast import mda

TEXT_2016 = """<<<PAGE 1>>>
CARGOJET INC.
Management’s Discussion and Analysis
Of Financial Condition and Results of Operations
For the Three and Six Month Periods Ended June 30, 2016
<<<PAGE 3>>>
The effective date of the MD&A is August 15, 2016. The condensed consolidated interim financial
<<<PAGE 5>>>
Overview
Financial Information and Operating Statistics Highlights
(Canadian dollars in millions, except where indicated)
Three Month Period Ended Six Month Period Ended
June 30, June 30,
2016 2015 Change % 2016 2015 Change %
Financial information
Revenues $79.3 $75.2 $4.1 5.5% $156.2 $129.3 $26.9 20.8%
Direct expenses $58.4 $67.9 ($9.5) -14.0% $118.7 $121.0 ($2.3) -1.9%
Selling, general & administrative expenses $8.9 $9.0 ($0.1) -1.1% $17.7 $16.8 $0.9 5.4%
Net Income (loss) $3.8 ($6.1) $9.9 162.3% $8.2 ($14.4) $22.6 156.9%
Earnings (loss) per share - $CAD
Basic $0.36 ($0.64) $1.00 156.3% $0.80 ($1.54) $2.34 151.9%
Diluted $0.36 ($0.64) $1.00 156.3% $0.78 ($1.54) $2.32 150.6%
EBITDA(1) $23.0 $6.3 $16.7 265.1% $46.4 $5.3 $41.1 775.5%
Adjusted EBITDA(1) $22.5 $6.6 $15.9 240.9% $39.9 $5.8 $34.1 587.9%
Operating statistics
Operating days(2) 50 50 - - 100 99 1 1.0%
Average cargo revenue per operating day(3) $1.23 $1.10 $0.13 11.8% $1.21 $1.02 $0.19 18.6%
Block hours 5,909 6,165 (256) -4.2% 11,942 10,790 1,152 10.7%
Aircraft in operating fleet
B727-200 6 9 (3) 6 9 (3)
B757-200 5 5 - 5 5 -
B767-200 1 4 (3) 1 4 (3)
B767-300 8 6 2 8 6 2
Challenger 601 1 - 1 1 - 1
21 24 (3) -12.5% 21 24 (3) -12.5%
Average number of full-time equivalent
employees 711 655 56 8.5% 711 655 56 8.5%
3 of 38
<<<PAGE 15>>>
Diluted $0.36 $(0.64) $0.78 $(1.54)
Average number of shares - basic (in thousands of shares) 10,476 9,482 10,310 9,354
Average number of shares - diluted (in thousands of shares) 10,640 9,482 10,474 9,354
<<<PAGE 16>>>
Jun 30 Mar 31 Dec 31 Sep 30 Jun 30 Mar 31 Dec 31 Sep 30
2016 2016 2015 2015 2015 2015 2014 2014
Revenues $79.3 $76.9 $84.4 $75.3 $75.2 $54.1 $57.1 $47.2
- Basic $0.36 $0.43 $(0.15) $(0.22) $(0.64) $(0.90) $(0.54) $(0.25)
- Diluted $0.36 $0.43 $(0.15) $(0.22) $(0.64) $(0.90) $(0.54) $(0.25)
Average number of shares -
diluted 10,640 10,135 10,094 9,928 9,482 9,224 9,150 9,090
(in thousands of shares)
<<<PAGE 19>>>
Core Overnight Revenues 51.1 49.0 2.1 4.3%
ACMI Revenues 7.0 2.6 4.4 169.2%
All-in Charter Revenues 3.5 3.5 - -
Total overnight, ACMI and charter revenues 61.6 55.1 6.5 11.8%
Total Revenue - FBO 0.1 0.1 - -
Total fuel and other cost pass through 17.1 19.5 (2.4) -12.3%
Fuel surcharge and other pass through revenues 17.2 19.6 (2.4) -12.2%
Lease and other revenue 0.5 0.5 - -
Tota l revenues 79.3 75.2 4.1 5.5%
Operating Days 50 50 - -
Direct expenses
Fuel Costs 13.7 19.9 (6.2) -31.2%
Depreciation 8.5 6.4 2.1 32.8%
Aircraft Cost 5.5 10.2 (4.7) -46.1%
Heavy Maintenance Amortization 1.7 1.7 - -
Maintenance Cost 5.6 5.2 0.4 7.7%
Crew Costs 5.3 5.8 (0.5) -8.6%
Commercial and Other Costs 18.1 18.7 (0.6) -3.2%
Total direct expenses 58.4 67.9 (9.5) -14.0%
Fuel costs were $13.7 million for the three month period ended June 30, 2016 compared to $19.9
<<<PAGE 24>>>
Review of Operations for the Six Month Periods ended June 30, 2016 and 2015
Core Overnight Revenues 100.4 87.6 12.8 14.6%
ACMI Revenues 13.8 5.9 7.9 133.9%
All-in Charter Revenues 7.0 7.4 (0.4) -5.4%
Total overnight, ACMI and charter revenues 121.2 100.9 20.3 20.1%
Total Revenue - FBO 0.2 0.2 - -
Fuel surcharge and other pass through revenues 33.9 27.5 6.4 23.3%
Lease and other revenue 1.1 0.9 0.2 22.2%
Total revenues 156.2 129.3 26.9 20.8%
Direct expenses
Fuel Costs 26.4 32.4 (6.0) -18.5%
Depreciation 16.1 10.9 5.2 47.7%
Aircraft Cost 13.9 19.4 (5.5) -28.4%
Heavy Maintenance Amortization 3.5 3.0 0.5 16.7%
Maintenance Cost 11.0 10.8 0.2 1.9%
Crew Costs 11.1 10.7 0.4 3.7%
Commercial and Other Costs 36.7 33.8 2.9 8.6%
Total direct expenses 118.7 121.0 (2.3) -1.9%
"""

TEXT_2026 = """<<<PAGE 1>>>
Management’s Discussion and Analysis
of Financial Condition and Results of Operations
For the Three and Six Month Periods Ended June 30, 2026
<<<PAGE 3>>>
2. FINANCIAL INFORMATION AND OPERATING STATISTICS HIGHLIGHTS……………. 4
<<<PAGE 4>>>
located at 2281 North Sheridan Way, Mississauga, Ontario, L5K 2S3. The MD&A was approved by the
Board of Directors and authorized for issuance on August 10, 2026. The condensed consolidated
<<<PAGE 6>>>
2. FINANCIAL INFORMATION AND OPERATING STATISTICS HIGHLIGHTS
(Canadian dollars in millions, except where indicated)
Three Month Periods Ended Six Month Periods Ended
June 30, June 30,
2026 2025 Change % 2026 2025 Change %
Domestic network, ACMI and charter
revenues $219.9 $204.6 $15.3 7.5% $437.0 $414.8 $22.2 5.4%
Fuel surcharge and other revenues $58.5 $38.3 $20.2 52.7% $104.1 $83.5 $20.6 24.7%
Total revenues excluding amortization $278.4 $242.9 $35.5 14.6% $541.1 $498.3 $42.8 8.6%
Amortization of contract assets ($2.6) ($4.7) $2.1 (44.7%) ($10.6) ($10.2) ($0.4) 3.9%
Total revenues $275.8 $238.2 $37.6 15.8% $530.5 $488.1 $42.4 8.7%
Direct expenses $216.6 $188.7 $27.9 14.8% $430.9 $385.8 $45.1 11.7%
Selling, general and administrative
expenses $25.8 $23.6 $2.2 9.3% $50.5 $39.9 $10.6 26.6%
Net earnings (loss) $7.0 ($3.2) $10.2 318.8% $11.1 $44.8 ($33.7) (75.2%)
Adjusted net earnings(1) $10.0 $15.7 ($5.7) (36.3%) $18.9 $41.0 ($22.1) (53.9%)
Earnings (loss) per share
Basic $0.47 ($0.21) $0.68 323.8% $0.74 $2.89 ($2.15) (74.4%)
Diluted $0.47 ($0.21) $0.68 323.8% $0.74 $2.80 ($2.06) (73.6%)
Adjusted(1) $0.67 $1.02 ($0.35) (34.3%) $1.27 $2.64 ($1.37) (51.9%)
Adjusted EBITDA (1) $87.3 $80.2 $7.1 8.9% $169.2 $161.0 $8.2 5.1%
Operating statistics (2)
Operating days (3) 50 50 - 0.0% 99 99 - 0.0%
Block hours (5) 15,787 15,840 (53) (0.3%) 32,781 33,185 (404) (1.2%)
B757-200 16 17 (1) 16 17 (1)
B767-200 2 3 (1) 2 3 (1)
B767-300 23 23 - 23 23 -
Cargo operating fleet 41 43 (2) (4.7%) 41 43 (2.0) (4.7%)
Head count 1,847 1,817 30 1.7% 1,847 1,817 30 1.7%
<<<PAGE 14>>>
Domestic network revenues $110.6 $102.3 $8.3 8.1%
ACMI revenues 54.6 62.5 (7.9) (12.6%)
All-in charter revenues 54.7 39.8 14.9 37.4%
Total domestic network, ACMI and charter revenues 219.9 204.6 15.3 7.5%
Fuel surcharge and other revenues 58.5 38.3 20.2 52.7%
Amortization of contract assets (2.6) (4.7) 2.1 (44.7%)
Total revenues 275.8 238.2 37.6 15.8%
<<<PAGE 15>>>
Three Month Periods Ended
(Canadian dollars in millions) June 30, CHANGE
2026 2025 $ %
Fuel costs 69.0 47.3 21.7 45.9%
Depreciation 43.7 38.5 5.2 13.5%
Aircraft cost 8.1 4.5 3.6 80.0%
Heavy maintenance amortization 7.5 5.6 1.9 33.9%
Maintenance cost 23.7 20.0 3.7 18.5%
Crew costs 19.6 29.0 (9.4) (32.4%)
Ground services 20.9 21.1 (0.2) (0.9%)
Airport services 12.7 10.8 1.9 17.6%
Navigation and insurance 11.4 11.9 (0.5) (4.2%)
Total direct expenses 216.6 188.7 27.9 14.8%
<<<PAGE 33>>>
Calculation of Adjusted Earnings and Adjusted EPS $ $ $ $
Net earnings (loss) 7.0 (3.2) 11.1 44.8
Adjusted net earnings 10.0 15.7 18.9 41.0
Weighted average number of shares - basic (in millions of shares) 14.9 15.4 14.9 15.5
Adjusted EPS 0.67 1.02 1.27 2.64
"""


def _row(line: str) -> mda.Row:
    rows = mda.parse_rows(line)
    assert len(rows) == 1
    return rows[0]


class TestParseRows:
    def test_parentheses_dollar_and_percent(self):
        r = _row("Adjusted(1) $0.67 $1.02 ($0.35) (34.3%) $1.27 $2.64 ($1.37) (51.9%)")
        assert r.key == "adjusted"
        assert r.vals == [0.67, 1.02, -0.35, -34.3, 1.27, 2.64, -1.37, -51.9]

    def test_footnote_after_label_is_not_a_value(self):
        r = _row("Block hours (5) 15,787 15,840 (53) (0.3%) 32,781 33,185 (404) (1.2%)")
        assert r.key == "blockhours" and r.vals[:3] == [15787, 15840, -53]

    def test_negative_change_in_fleet_row_is_kept(self):
        r = _row("B727-200 6 9 (3) 6 9 (3)")
        assert r.key == "b727200" and r.vals == [6, 9, -3, 6, 9, -3]

    def test_model_number_belongs_to_label(self):
        assert _row("Cessna 750 1 - 1 - 1 - 1 -").vals[:2] == [1, 0.0]
        assert _row("Challenger 601 2 2 - - 2 2 - -").key == "challenger601"

    def test_split_word_and_nil_dash(self):
        assert _row("Tota l revenues 79.3 75.2 4.1 5.5%").key == "totalrevenues"
        assert _row("Diluted $(0.12) $ - ($0.12) -100.0%").vals == [-0.12, 0.0, -0.12, -100.0]

    def test_wrapped_label_joins_previous_line(self):
        rows = mda.parse_rows("Domestic network, ACMI and charter\nrevenues $219.9 $204.6 $15.3 7.5%")
        assert rows[0].key == "domesticnetworkacmiandcharterrevenues"

    def test_stray_dash_between_columns_is_dropped(self):
        r = _row("Total fuel surcharge and other revenues 61.6 - 78.2 (16.6) -21.2%")
        assert r.vals == [61.6, 78.2, -16.6, -21.2]
        # riktig nolla i föregående år ska stå kvar
        assert _row("Total Revenue - FBO 0.1 - 0.1 -").vals == [0.1, 0.0, 0.1, 0.0]

    def test_prose_is_not_a_row(self):
        assert mda.parse_rows("Fuel costs were $13.7 million for the three month period") == []


@pytest.fixture(scope="module")
def p2016():
    return mda.parse_mda(TEXT_2016)


@pytest.fixture(scope="module")
def p2026():
    return mda.parse_mda(TEXT_2026)


class TestEra2016:
    def test_current_quarter(self, p2016):
        c = p2016["cur"]
        assert (c["block_hours"], c["operating_days"]) == (5909, 50)
        assert (c["domestic_rev"], c["acmi_rev"], c["charter_rev"], c["core_rev"]) == (51.1, 7.0, 3.5, 61.6)
        # pass-through (inkl. FBO) + lease and other = senare "Fuel surcharge and other revenues"
        assert c["fuel_surcharge_other_rev"] == 17.7
        assert c["amortization_contract_assets"] is None
        assert (c["total_rev"], c["direct_expenses"], c["fuel_costs"]) == (79.3, 58.4, 13.7)
        assert (c["adj_ebitda"], c["eps_diluted"], c["adj_eps"]) == (22.5, 0.36, None)
        assert (c["fleet_operating"], c["fleet_b757"], c["fleet_b767_200"], c["fleet_b767_300"]) == (21, 5, 1, 8)
        assert p2016["mda_date"] == "2016-08-15"
        assert any("Challenger-601=1" in n and "B727-200=6" in n for n in p2016["notes"])

    def test_bridge_fields(self, p2016):
        c = p2016["cur"]
        assert (c["sga"], c["net_earnings"], c["depreciation_in_direct"]) == (8.9, 3.8, 8.5)
        assert (c["adj_earnings"], c["diluted_shares_m"]) == (None, 10.64)
        bd = p2016["direct_breakdown"]
        assert list(bd) == ["Fuel costs", "Depreciation", "Aircraft cost", "Heavy maintenance amortization",
                            "Maintenance cost", "Crew costs", "Commercial and other costs"]
        assert round(sum(bd.values()), 1) == 58.4
        assert p2016["prior"]["diluted_shares_m"] == 9.482
        assert p2016["prev_q_diluted_shares_m"] == 10.135

    def test_prior_year_and_ytd_columns(self, p2016):
        assert (p2016["prior"]["block_hours"], p2016["prior"]["eps_diluted"], p2016["prior"]["total_rev"]) == (6165, -0.64, 75.2)
        assert (p2016["ytd"]["block_hours"], p2016["ytd"]["total_rev"], p2016["ytd"]["core_rev"]) == (11942, 156.2, 121.2)
        assert p2016["ytd"]["fuel_surcharge_other_rev"] == 35.0
        assert p2016["prev_q_eps"] == 0.43

    def test_derive_q1_from_six_months(self, p2016):
        rec = mda.derive_from_6m("2016Q1", "2016Q2", p2016)
        assert (rec["block_hours"], rec["operating_days"], rec["total_rev"]) == (6033, 50, 76.9)
        assert (rec["core_rev"], rec["fuel_surcharge_other_rev"], rec["domestic_rev"]) == (59.6, 17.3, 49.3)
        assert (rec["direct_expenses"], rec["fuel_costs"], rec["adj_ebitda"]) == (60.3, 12.7, 17.4)
        assert rec["eps_diluted"] == 0.43 and rec["fleet_operating"] is None
        # nettoresultatet 4.4 stämmer med kvartalstabellens Mar 31 2016-kolumn
        assert (rec["sga"], rec["net_earnings"], rec["depreciation_in_direct"]) == (8.8, 4.4, 7.6)
        assert rec["diluted_shares_m"] == 10.135
        assert '"Fuel costs": 12.7' in rec["direct_expenses_breakdown_json"]
        # jämförelsetal 2015Q1 = 6M 2015 − Q2 2015; total 54.1 = kvartalstabellens Mar 31 2015
        assert (rec["py_block_hours"], rec["py_total_rev"], rec["py_core_rev"]) == (4625, 54.1, 45.8)
        assert (rec["py_fuel_costs"], rec["py_adj_ebitda"], rec["py_adj_eps"]) == (12.5, -0.8, None)
        assert rec["derived"].startswith("6M−Q2")
        assert mda.row_checks(rec) == []

    def test_title_check(self):
        assert mda.mentions_period_end(TEXT_2016, "2016Q2")
        assert not mda.mentions_period_end(TEXT_2016, "2016Q3")


class TestEra2026:
    def test_current_quarter(self, p2026):
        c = p2026["cur"]
        assert (c["block_hours"], c["operating_days"]) == (15787, 50)
        assert (c["domestic_rev"], c["acmi_rev"], c["charter_rev"], c["core_rev"]) == (110.6, 54.6, 54.7, 219.9)
        assert (c["fuel_surcharge_other_rev"], c["amortization_contract_assets"], c["total_rev"]) == (58.5, -2.6, 275.8)
        assert (c["direct_expenses"], c["adj_ebitda"], c["adj_eps"], c["eps_diluted"]) == (216.6, 87.3, 0.67, 0.47)
        assert (c["fleet_operating"], c["fleet_b757"], c["fleet_b767_200"], c["fleet_b767_300"]) == (41, 16, 2, 23)
        assert p2026["mda_date"] == "2026-08-10"
        # sammanfattning och intäktstabell stämmer; enda anteckningen gäller aktieantalet
        assert p2026["notes"] == ["diluted shares not reported; basic weighted avg 14.9M"]

    def test_bridge_fields(self, p2026):
        c = p2026["cur"]
        assert (c["sga"], c["net_earnings"], c["adj_earnings"]) == (25.8, 7.0, 10.0)
        assert (c["depreciation_in_direct"], c["diluted_shares_m"]) == (43.7, None)
        assert p2026["prior"]["adj_earnings"] == 15.7
        bd = p2026["direct_breakdown"]
        assert "Commercial and other costs" not in bd and bd["Navigation and insurance"] == 11.4
        assert round(sum(bd.values()), 1) == 216.6

    def test_prior_year_column(self, p2026):
        pr = p2026["prior"]
        assert (pr["block_hours"], pr["adj_eps"], pr["amortization_contract_assets"]) == (15840, 1.02, -4.7)
        assert pr["fleet_operating"] == 43

    def test_record_passes_sum_checks(self, p2026):
        rec = mda.record_from_mda("2026Q2", p2026)
        assert mda.row_checks(rec) == []


_SUMMARY_2021Q2 = """<<<PAGE 6>>>
Financial Information and Operating Statistics Highlights
2021 2020 Change % 2021 2020 Change %
Revenues $172.1 $196.1 ($24.0) -12.2% $332.4 $319.1 $13.3 4.2%
Direct expenses $117.2 $105.4 $11.8 11.2% $232.2 $196.2 $36.0 18.3%
Adjusted EBITDA (1) $67.4 $80.2 ($12.8) -16.0% $131.6 $124.8 $6.8 5.4%
Operating days (2) 50 50 - - 100 100 - -
Block hours 13,454 14,119 (665) -4.7% 26,550 23,459 3,091 13.2%
Average head count 1,398 1,136 262 23.1% 1,398 1,136 262 23.1%
<<<PAGE 16>>>
Domestic Network Revenues 79.7 69.3 10.4 15.0%
ACMI Revenues 37.2 32.7 4.5 13.8%
All-in Charter Revenues 17.5 77.4 (59.9) -77.4%
Total domestic network, ACMI and charter revenues 134.4 179.4 (45.0) -25.1%
Total Revenue - Fixed based operations revenues 0.5 - 0.5 500.0%
Total fuel and other cost pass through revenues 33.1 13.3 19.8 148.9%
Fuel surcharge and other pass through revenues 33.6 13.3 20.3 152.6%
Other revenues 4.1 3.4 0.7 20.6%
Tota l revenues 172.1 196.1 (24.0) -12.2%
"""

_SUMMARY_2022Q2 = """<<<PAGE 7>>>
Financial Information and Operating Statistics Highlights
2022 2021 Change % 2022 2021 Change %
Revenues $246.6 $172.1 $74.5 43.3% $480.2 $332.4 $147.8 44.5%
Direct expenses 185.5 117.2 68.3 58.3% 352.2 232.2 120.0 51.7%
Diluted 9.12 (0.64) 9.76 1525.0% 5.93 4.42 1.51 34.2%
Adjusted (1) 1.51 1.36 0.15 11.0% 3.27 1.79 1.48 82.7%
Adjusted EBITDA (2) 81.1 67.4 13.7 20.3% 164.1 131.6 32.5 24.7%
Operating days (5) 49 50 (1.0) -2.0% 99 100 (1.0) -1.0%
Block hours (7) 17,872 13,454 4,418 32.8% 35,573 26,550 9,023 34.0%
Head count 1,624 1,398 226 16.2% 1,624 1,398 226 16.2%
<<<PAGE 18>>>
Domestic network revenues $86.8 $75.5 $11.3 15.0%
ACMI revenues 60.4 37.2 23.2 62.4%
All-in charter revenues 27.6 22.4 5.2 23.2%
Total domestic network, ACMI and charter revenues 174.8 135.1 39.7 29.4%
Total revenue - fixed based operations 0.3 0.4 (0.1) -25.0%
Total fuel and other cost pass through 62.6 32.6 30.0 92.0%
Fuel surcharge and other pass through revenues 62.9 33.0 29.9 90.6%
Other revenue 8.9 4.0 4.9 122.5%
Total revenues 246.6 172.1 74.5 43.3%
Direct expenses
Fuel costs 64.3 25.2 39.1 155.2%
Depreciation 29.8 25.5 4.3 16.9%
Total direct expenses 185.5 117.2 68.3 58.3%
"""


def test_prior_year_columns_carry_restatement():
    """2022Q2:s jämförelsekolumn visar 2021Q2 efter segmentomklassningen, inte originalet."""
    orig = mda.record_from_mda("2021Q2", mda.parse_mda(_SUMMARY_2021Q2))
    rec = mda.record_from_mda("2022Q2", mda.parse_mda(_SUMMARY_2022Q2))
    assert (orig["domestic_rev"], orig["charter_rev"], orig["core_rev"]) == (79.7, 17.5, 134.4)
    assert (rec["py_domestic_rev"], rec["py_charter_rev"], rec["py_core_rev"]) == (75.5, 22.4, 135.1)
    # oförändrat mellan rapporterna
    assert (rec["py_acmi_rev"], rec["py_total_rev"], rec["py_block_hours"]) == (orig["acmi_rev"], 172.1, 13454)
    assert (rec["py_fuel_surcharge_other_rev"], rec["py_amortization_contract_assets"]) == (37.0, None)
    assert (rec["py_adj_ebitda"], rec["py_adj_eps"], rec["py_adj_earnings"]) == (67.4, 1.36, None)
    assert (rec["py_fuel_costs"], rec["py_depreciation_in_direct"], rec["py_direct_expenses"]) == (25.2, 25.5, 117.2)
    assert mda.row_checks(rec, "py_") == []
    # befintliga kolumner påverkas inte
    assert (rec["domestic_rev"], rec["block_hours"]) == (86.8, 17872)


def test_row_checks_flag_mismatch():
    rec = {"domestic_rev": 50.0, "acmi_rev": 7.0, "charter_rev": 3.5, "core_rev": 61.6,
           "fuel_surcharge_other_rev": 17.7, "amortization_contract_assets": None, "total_rev": 79.3}
    msgs = mda.row_checks(rec)
    assert len(msgs) == 1 and "core_rev" in msgs[0]


def test_restatement_note_against_q_plus_4():
    rec = mda._blank("2025Q2")
    rec["block_hours"] = 15812
    parsed = {"2026Q2": {"prior": {"block_hours": 15840.0}}}
    assert mda.restatement_notes("2025Q2", rec, parsed) == ["restated in 2026Q2: block_hours 15,840 vs orig 15,812"]
