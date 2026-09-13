# CJT-nowcast: Cargojets kvartal ur deras egna flygningar

Cargojet (TSX: CJT) är ett kapacitetsbolag. Intäkten är i huvudsak blocktimmar
× kontrakterade rater, och bränsle går vidare till kund. Blocktimmarna går att
mäta i ADS-B medan kvartalet pågår. Det här paketet mäter dem, validerar mot
bolagets rapporterade blocktimmar och översätter dem till en relativ prognos
för omsättning och justerad EBITDA.

**Ej finansiell rådgivning.**

## Köra

```bash
~/.local/bin/uv pip install --python .venv/bin/python -r requirements-nowcast.txt
python -m cjt_nowcast fleet        # Transport Canada-registret → flotta
python -m cjt_nowcast extract      # ADS-B-dagsdumpar → Cargojet-spår (~1 dygn första gången)
python -m cjt_nowcast health       # täckning per kvartal + upptäckta plan
python -m cjt_nowcast flights      # spår → flygningar med blocktid
python -m cjt_nowcast routes       # ruttabell → fyll i manual/route_classes.csv
python -m cjt_nowcast mda          # MD&A Q1 2016–Q2 2026 → facit
python -m cjt_nowcast market       # jet fuel, USD/CAD, CJT.TO
python -m cjt_nowcast consensus    # manual/consensus.csv (+ skrapat)
python -m cjt_nowcast validate     # ADS-B-timmar mot rapporterade (mål ±5 %)
python -m cjt_nowcast backtest     # de 9 testerna 2024Q2–2026Q2 med RAPPORTERADE blocktimmar (--hours adsb senare)
python -m cjt_nowcast surprise     # samma tabell: prognos ÖVER/UNDER konsensus → utfall → rätt?
python -m cjt_nowcast sample       # snabbläge: Q3-nowcast på veckodagsmatchade stickprovsdagar
python -m cjt_nowcast level        # nivåkontroll: ADS-B-timmar (uppskalade) mot rapporterade per kvartal
python -m cjt_nowcast track        # löpande insamling av Q3-blocktimmar per dag (körs med nohup)
python -m cjt_nowcast ui           # andra UI:t → http://127.0.0.1:8061 (Atlas-trackern kör på 8060)
python -m cjt_nowcast nowcast      # 2026Q3 på alla dagar (kräver full extraktion)
.venv/bin/python -m pytest tests/test_nowcast_*.py
```

**Reproducerbarhet.** Varje externt svar sparas som det kom i `data/raw/` och
loggas i `data/raw/_manifest.jsonl` med url, sha256 och tidpunkt. Senare
körningar hämtar aldrig om, utom med `--refresh`. Källor som växer dagligen
(register, EIA, BoC, Yahoo) sparas som daterade ögonblicksbilder. Allt i
`data/processed/` går att bygga om offline ur `raw/` och `manual/`.
`manual/` är incheckat och innehåller det som inte går att skrapa, med källa
per rad.

## Datakällor

| Data | Källa | Licens / not |
|---|---|---|
| ADS-B-spår | adsb.lol `globe_history`, dagsdumpar på GitHub | ODbL 1.0, attribution krävs |
| Flotta | Transport Canada CCAR + upptäckt i ADS-B (CJT-callsign / ownOp) | öppen data |
| Flygplatser | OurAirports | public domain |
| Facit | Cargojets MD&A, cargojet.com (2021–) och Wayback (2016–2020) | publika rapporter |
| Jet fuel | EIA U.S. Gulf Coast Kerosene-Type Jet Fuel spot (EER_EPJK_PF4_RGC_DPG) | proxy för Platts |
| USD/CAD | Bank of Canada Valet: FXUSDCAD (2017–), IEXE0101 (2016) | |
| Kurs | stooq CSV-API (`/q/d/l/`) | API-nyckel i `.env`; symbol sätts uttryckligen |
| Konsensus | `manual/consensus.csv` (+ Zacks för rapportdatum) | se nedan |

## Antaganden och val

### ADS-B och blocktid

- **Varför dagsdumpar och inte HTTP per plan.** adsb.lol:s HTTP-sökväg per
  plan och dag fanns 2026-09-13 bara för jan 2024–sep 2025. Från ~dec 2025
  gav hela dagkataloger 404, och efter några hundra anrop svarade den 403.
  En 404 där betyder "dagen saknas på servern", inte "planet flög inte".
  Dumparna täcker hela perioden och ger en enhetlig tidsserie. Prod används,
  utom när staging är mer än 20 % större.
- **Bandbredd och förspel.** Bandbredden (~15 MB/s) begränsar, inte CPU.
  Därför hoppas dumpens förspel före `traces/` över (heatmaps m.m., ~23 %).
  Förspelet hittas med binärsökning via HTTP Range, cirka 20 anrop per dag,
  och `skipped_bytes` redovisas i dagmanifestet. Känns layouten inte igen
  strömmas hela dumpen.
- **Dagar utan ADS-B-dump.** 2025-12-14 till 17 och 2026-05-06 har bara
  MLAT-releaser. Kvartalssummor skalas upp för saknade dagar (`day_scaled`).
  Flygningar som korsar en saknad dag blir ofta härledda (se nedan), så
  uppskalningen dubbelräknar marginellt.
- **Blocktid** följer MD&A:s definition: från att bromsarna släpps vid
  uppställningsplatsen till att de sätts. Den mäts som sista stillastående
  markpunkt (≥5 min, ≤1,5 kt) före start och första stillastående efter
  landning, när taxikedjan har obruten täckning (luckor ≤5 min).
- **Taxitid** där marken inte syns: median per flygplats ur fullt
  observerade kedjor (≥8 observationer), annars global median. Navet YHM
  saknar ofta markpunkter, särskilt 2024.
- **Start och landning** räknas som observerade om mark- och luftpunkt ligger
  inom 5 min från varandra. Annars skattas de ur låga punkter (inflygning
  800 ft/min, stigning 2 000 ft/min) eller ur första och sista luftpunkt vid
  280 kt.
- **Härledda ben.** Om planet senast sågs på A och nästa gång syns på B ≠ A
  måste det ha flugit. Benet läggs då in med en distansmodell (luftburen tid
  ≈ a + b·km, skattad på observerade flygningar) och flaggas `inferred`.
  Osedda tur-och-retur-rotationer (A→B→A) går inte att upptäcka. De är den
  kvarvarande systematiska underskattningen, och därför kalibreras nivån.
- **Fantomflygningar**, alltså samma fält och <15 min i luften, räknas bort.
  Enstaka felaktiga höjdrapporter delar aldrig ett besök.
- **Överflygningar är inga besök.** Ett besök utan markpunkter måste gå
  under 1 500 ft över fältet. Uppmätt: inflygningen mot YHM passerar YKF på
  ~2 500 ft och gav annars ett falskt ben.
- **Linjefält före småfält.** Fält utan IATA-kod inom 6 km från ett högre
  prioriterat fält tas bort ur uppslaget, liksom OurAirports dubblettposter.
  Uppmätt: "CA-1291" = "(Duplicate)YEG" fick annars YEG:s markpunkter. För
  besök utan markpunkter väljs stor+IATA före medel+IATA före övrigt inom
  25 km.
- **Tur och retur med osedd mellanlandning.** Samma fält, >150 km bort och
  >1 h i luften (uppmätt: MIA→MIA 7–16 h när Latinamerika saknar täckning)
  räknas som två cykler. Blocktiden är total tid minus medianvändtiden på
  utestation, skattad ur data (0,5–6 h mellan on-block och nästa off-block;
  2,4 h på testdagen). Flerstoppsrotationer överskattas då med en vändtid
  per extra stopp. Flaggas `roundtrip_inferred`.
- **Kvartal** tilldelas efter off-block i östkusttid (America/Toronto).
- **Flottan** är alla hex som någon dag flugit med CJT-callsign eller har
  ownOp "Cargojet". Det fångar plan som lämnat registret (t.ex. C-GCJN,
  C-FCJU, C-FGAJ, C-GVAJ 2024) och inhyrda plan. Kanadensisk hex är en ren
  funktion av registreringen (verifierat mot registret).

### Segmentklassning

Klassreglerna är ditt beslut. `routes` skriver `data/processed/route_table.csv`
med alla rutter, volym, nattandel och ett förslag. Det som räknas är det som
står i `manual/route_classes.csv`. Oklassat syns som `unclassified` och göms
aldrig i ett segment. Förslagen följer specen: domestic = inrikes Kanada,
ACMI = CVG/MIA söderut (DHL), charter = transpacifiskt och LGG–TLV. Gråzoner
föreslås som "?": transborder, transatlantiskt från Kanada, US-inrikes,
Newark–Bermuda och positionering.

### Modell

- **Relativ, inte absolut.** Modellen arbetar med YoY-förändringar.
  Nivåfelet i ADS-B tar i stort sett ut sig när ADS-B jämförs mot ADS-B för
  samma kalenderdagar året innan.
- **Omsättning.** y = YoY kärnomsättning (domestic + ACMI + charter, exkl.
  bränsletillägg). X = YoY blocktimmar, jet fuel (kvartalsmedel) och
  USD/CAD. OLS jämförs mot Ridge (standardiserat X, alfa på
  tidsserie-korsvalidering). Ridge väljs vid konditionstal >30 eller minst
  3 % bättre CV-fel.
- **Asymmetri i timmarna.** MD&A rapporterar bara totala blocktimmar, inte
  per segment. Träningen (2017–) sker därför på total-YoY, medan prognosen
  segmentviktar ADS-B-timmarna med förra årets segmentintäkter när minst
  80 % av timmarna är klassade.
- **2024-kvartalen** saknar ADS-B året innan. Där jämförs den kalibrerade
  ADS-B-nivån (median av reported/ADS-B för tidigare kvartal) mot
  rapporterade timmar året innan. 2024Q1 är okalibrerad och flaggas.
- **Jämförbara fjolårssiffror.** Träningen använder fjolårskolumnen i varje
  kvartals egen MD&A (`py_*`, omräknad). Cargojet flyttade intäkter mellan
  segment 2021→2022 och omdefinierade justerad EBITDA 2020–2022, så q mot
  ursprungligen rapporterat q−4 mäter definitionsbyten. Prognosbasen är
  däremot det ursprungligen rapporterade, eftersom det var känt före
  rapporten.
- **COVID-kvartalen 2020Q2–2021Q4 ingår inte i träningen** som standard. De
  är ett strukturbrott: med dem blev CV-felet 18–20 procentenheter YoY, utan
  dem 7,8. Modellen med alla kvartal redovisas som diagnos.
- **Bränsletillägg** = k · jet fuel i CAD · timmar, där k är medianen av de
  fyra senast rapporterade kvartalen.
- **Amortering av kontraktstillgångar** = medianen av de fyra senaste
  kvartalen, med saknat räknat som 0. Före 2023Q4 låg posten inbakad i
  bränsletillägget. Det senaste värdet ensamt är fel, eftersom 2023Q4 hade
  −32,8 i engångsuppkomst.
- **Justerad EBITDA**, specens brygga:
  ΔEBITDA = Δtimmar·(intäkt/h − direkt kontantkostnad/h)
  + pris/mix − Δkostnadssteg + bränslelag + drift.
  - Direkt kontantkostnad = direkta kostnader − bränsle − avskrivningar i
    direkta kostnader. Bränsle går vidare till kund och avskrivningar ingår
    inte i EBITDA.
  - Intäkt och kostnad per timme tas från samma kvartal året innan.
  - Drift = medelresidual de 8 senaste kvartalen. Den fångar kostnadsinflation
    och kontraktsändringar som inte är kända kostnadssteg.
- **Kalibrerad brygga (huvudvariant).** ΔEBITDA + kostnadssteg = a·volym +
  b·pris/mix + c, skattat på de 12 senaste kvartalen före prognoskvartalet.
  Specens brygga antar att pris/mix når EBITDA fullt ut och att kostnad per
  timme står still. Kontantkostnaden per timme steg dock från cirka 5,1 till
  6,5 tusen CAD 2023–2026. Med rapporterade timmar 2024Q1–2026Q2 hade
  specens brygga + drift 13,6 % medelfel och den kalibrerade 7,1 %. Båda
  redovisas.
- **Direkt kostnad per blocktimme** rapporteras inte av Cargojet. Den räknas
  som direkta kostnader / blocktimmar.
- **Pilotavtalet** tolkas som en KOSTNAD på +4,5 mkr CAD per kvartal
  (mittpunkten av 4–5) från 2026Q3, i `manual/cost_steps.csv`. Det slår mot
  YoY i fyra kvartal, 2026Q3–2027Q2.
- **Bränslelag.** β skattas bara på kvartal där jet fuel rört sig mer än 10 %
  inom kvartalet (medel sista mot första 20 handelsdagarna), som
  residual-EBITDA/kärnomsättning mot rörelsen, genom origo. N och t-värde
  redovisas alltid, eftersom N blir litet.
- **EPS** ≈ fjolårets justerade EPS + ΔEBITDA·(1−26,5 %)/aktier, där aktier
  = justerat resultat / justerad EPS. Det ignorerar förändrade avskrivningar
  och räntor och används bara för riktningen mot konsensus.

### Backtest och informationsdisciplin

- **Endast data före rapporten.** Prognosen för q använder bara det som fanns
  före q:s rapport:
  - modellen skattas på kvartal < q
  - blocktimmar kommer ur ADS-B, aldrig ur q:s MD&A
  - kalibreringen tas från kvartal < q
  - marknadsdata avser hela q
- **Diagnos med rapporterade timmar.** Samma modell körs också med
  rapporterade timmar ("perfect hours"). Det skiljer mätfel från modellfel.
- **Kursreaktion.** Cargojet rapporterar efter stängning, så reaktionen mäts
  som nästa handelsdags stängning mot rapportdagens.
- **Kursfil (förstahand).** Kursen läses från `manual/cjt_prices.csv`, en
  manuellt nedladdad CSV med daglig kurs för CJT på TSX från Investing.com,
  Yahoo Finance eller StockAnalysis. Formatet känns igen automatiskt. Den
  ojusterade stängningskursen används, eftersom reaktionen mäts över en natt.
  Filen behöver täcka minst 2024-08-01–2026-08-31.
- **Kurskälla (reserv).** Finns ingen kursfil hämtas kursen från stooq. Nyckeln krävs sedan 2026 och fås
  via CAPTCHA på `https://stooq.com/q/d/?s=spy.us&get_apikey`. Den läggs i
  `.env` som `STOOQ_API_KEY`, tillsammans med `CJT_STOOQ_SYMBOL`. Stooq
  dokumenterar inte Toronto-suffixet, så symbolen gissas inte. Stooq svarar
  HTTP 200 även vid fel och sajten har en JS-kontroll, så varje svar valideras
  som kurs-CSV innan det cachas. Nyckeln maskeras i hämtmanifestet.

### Konsensus

`manual/consensus.csv` är användarens export från Investing.com (CAD, dagen
före rapport). Skrapning prövades först och togs bort. Uppmätt 2026-09-13
hade Zacks (CGJTF) ingen konsensus, och Investing.com blockerade anrop.

- **2024Q1 saknas** hos Investing.com. MarketBeat har siffror, men det är en
  annan vendor och de blandas inte in. Kvartalet ingår därför inte i
  träffräkningen.
- **2026Q3 är forward-konsensus** per 2026-09-11, inte dagen före rapport.
  Det står i nowcast-rapporten.
- **Utfall tas alltid ur MD&A.** Filens utfallsomsättning stämmer exakt mot
  MD&A. Filens "actual EPS" för 2025Q1 (2,87) är däremot IFRS utspädd EPS,
  inte justerad (1,62). `python -m cjt_nowcast consensus` listar sådana
  avvikelser.
- **Kursreaktionen förankras i MD&A-datumet.** Investing.coms rapportdatum
  ligger +1 dag mot MD&A 2025Q1–Q3. Fönstret stängning(MD&A-dag) →
  stängning(nästa handelsdag) fångar både släpp efter stängning samma dag och
  före öppning nästa dag.

### Q3 2026

Kvartalet pågår. Nowcasten använder ADS-B till och med senast publicerade
dagsdump och antar att resten av kvartalet har samma YoY som de uppmätta
dagarna. Antalet dagar och täckningen står i rapporten.

## Status 2026-09-13

**Historiken (2024Q2–2026Q2) använder Cargojets rapporterade blocktimmar**
som indata, inte ADS-B. Modellen skattas bara på kvartal före det testade.
Resultat mot konsensus:
- **Omsättning:** 8/9 rätt riktning.
- **Just. EPS:** 3/9 rätt riktning.
- **Just. EBITDA:** kan inte poängsättas, eftersom EBITDA-konsensus saknas
  i `manual/consensus.csv`.

Resultatet visar att blocktimmar förklarar omsättningen. Det bevisar inte att
ADS-B mäter dem tillräckligt bra före rapporten.

**Nivåkontroll ADS-B mot rapporterat** (`level`, veckodagsuppskalade hela
veckor):

| Kvartal | Dagar | Avvikelse |
|---|---:|---:|
| 2025Q2 | 31 | −4,5 % |
| 2025Q3 | 28 | −10,2 % |
| 2026Q2 | 18 | −15,7 % |

ADS-B ligger konsekvent lägre, men kvoten varierar 13 % mellan kvartalen. Den
kan därför inte kalibreras bort med en fast faktor än, och Q3-signalen ur ADS-B
är en indikation, inte en validerad prognos. Två regler i segmenteringen
flyttade nivån mest och är införda:
- rundturer begränsas till en tur och retur till längsta observerade punkten
- flygningar som varit mer än dubbla modelltiden plus 1 h i luften ersätts
  med distansmodellen

**Löpande Q3-insamling.** Insamlaren sparar blocktimmar per dag och segment i
`data/processed/block_hours_daily_2026Q3.csv`. Loggen ligger i
`data/logs/track_2026Q3.log`.
- Start: `nohup .venv/bin/python -u -m cjt_nowcast track --quarter 2026Q3 >> cjt_nowcast/data/logs/track_2026Q3.log 2>&1 &`
- Stopp: `kill $(cat cjt_nowcast/data/track_2026Q3.pid)`
- Omstart efter omstart av datorn: kör startkommandot igen. Allt som redan är
  hämtat hoppas över.

## Kända begränsningar

- Community-ADS-B har sämre täckning över hav och i delar av Latinamerika.
  Charter och ACMI mäts därför sämre än domestic. Andelen härledda timmar
  redovisas per kvartal.
- Flottupptäckten via callsign fångar inte Cargojet-plan som flyger under
  kundens callsign med utländsk hex. Kanadensiska Cargojet-plan fångas alltid
  via ownOp.
- EIA USGC jet är en proxy. Cargojet köper mest bränsle i Kanada.
- Med få kvartal (≈38 YoY-observationer, varav COVID-åren) är
  koefficienterna osäkra. CV-RMSE redovisas.
