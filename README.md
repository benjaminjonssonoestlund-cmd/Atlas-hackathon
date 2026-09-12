# Atlas Chokepoint — sjöfartens flaskhalsar → marknaden

**Hackathon-bygge (finans).** En fokuserad efterföljare till atlas-earth:
i stället för 84 lager gör den EN sak ordentligt — mäter stress i
världshandelns 11 maritima flaskhalsar ur satellitdata och översätter den
till prisscenarier för kopplade råvaror och aktier. Samma 3D-glob och
recon-estetik som Atlas.

## Pitch (30 sekunder)

> 90 % av världshandeln går till sjöss, och nästan allt passerar elva nålsögon.
> När ett stryps rör sig priserna på olja, vete, frakt och halvledare — men
> signalen syns i satellitdata **dagar innan** den är förstasidesstoff.
> Atlas Chokepoint läser IMF:s satellit-AIS dagligen, larmar när ett sund
> avviker från sin baslinje och visar vad liknande episoder historiskt gjort
> med priserna — med ärliga konfidensband. Just nu: Hormuz stängt dag 180+,
> Röda havet-krisen inne på tredje året, Brent +6 % i veckan. Allt på skärmen
> kommer från öppna, nyckelfria källor.

## Vad den gör

1. **Stressindex 0–100 per sund** — dagligt tonnage (DWT) mot robust
   median/MAD-baslinje. Vid episodstart **fryses baslinjen** tills trafiken
   återhämtat sig, så att en långvarig kris inte "normaliseras" av sitt eget
   glidande fönster (utan detta gled Hormuz-stängningen tillbaka till
   "normalt" efter ett halvår).
2. **Episoddetektion** — riktad z ≥ 1,5 startar en episod. Motorn återfinner
   COVID-19, Ever Given, Ukraina-invasionen, Panama-torkan, Röda havet-krisen
   och Hormuz-stängningen 2026 ur rådatan, utan att ha fått dem berättade.
3. **Event-studie → prognosband** — varje kopplat instruments utfall
   +5/+10/+20 handelsdagar efter historiska episodstarter, som empiriska
   kvantiler (median, p10, p90) jämförda mot instrumentets basnivå.
   Analoger, inte regressioner; N visas alltid. **Ej finansiell rådgivning.**
4. **Glob-UI** — stressfärgade pulserande noder, omdirigeringsbågar
   (Suez/Bab-el-Mandeb → Godahoppsudden när omläggningen är aktiv),
   detaljpanel med sparkline, episodhistorik och scenarioband.

## Köra

```bash
pip install -r requirements.txt
python run.py                     # → http://127.0.0.1:8060

# Max antal fartyg (global AIS-prenumeration, kräver arbetsstation):
ATLAS_AIS_MODE=global ATLAS_AIS_MAX=200000 ATLAS_AIS_WRITE_EVERY=12 python run.py
```

Kopiera `.env.example` → `.env` för nycklar (AISStream, BarentsWatch).

Första starten bygger analysen (~30–60 s: 7 års satellit-AIS + prishistorik);
allt cachas sedan i `atlas_choke/data/cache.db`. Inga API-nycklar krävs.

Valfritt: `AISSTREAM_API_KEY` (gratis, aisstream.io) tänder live-fartygslagret —
men AISStreams ström har legat nere sedan aug 2026, så kärnan är byggd för att
inte bero på den.

```bash
python -m pytest tests/           # 12 motor-tester, inga nätverksanrop
```

## Live-lagret: max live-data, historik som baslinje

Rollfördelning (kärndesignen): **live-AIS är nuet**, **PortWatch-historiken är
normen** nuet jämförs mot, **event-studien är analogerna**. Live-bilden mergar
tre källor per MMSI (~10 000–15 000 fartyg):

| Källa | Täckning | Nyckel |
|---|---|---|
| AISStream (websocket, korridorläge) | 14 stora regioner längs världens farleder | gratis nyckel (opålitligt underhållen tjänst — degraderar snyggt) |
| Fintraffic Digitraffic (CC BY 4.0) | Östersjön inkl. svenska vatten | **nej** — verifierat live |
| BarentsWatch (NLOD 2.0) | Norska EEZ + Svalbard | gratis konto (valfri: `BARENTSWATCH_CLIENT_ID/SECRET`) |

`ATLAS_AIS_MODE`: `corridors` (standard, ~10-15k fartyg) · `chokepoints`
(lätt, ~2-3k) · `global` (allt aisstream har).

Ur live-bilden beräknas per sund, mot PortWatch-normen:
- **Fartyg i zonen + ankrade/förtöjda** (AIS NavigationalStatus 1/5 — riktig kö,
  inte bara låg fart)
- **Tonnage-proxy**: L×B×djupgående×ρ×c_b (IMF WP/19/275, öppen replikering i
  Världsbankens pacific-observatory) — "ton i vattnet", inte bara skrov
- **Live-passagetakt**: zoninträden räknas kontinuerligt (hormuz.now-metoden)
  → "vår live-takt vs IMF-normen"
- **Egen live-historik**: ring-buffert (14 d, 5-min-poster) gör att
  "nu vs igår" växer fram från dag ett
- **Dead reckoning i UI:t** (deck.gl/worldview-mönstret): fartygen glider
  längs kurs/fart mellan pollarna — globen fryser aldrig

## Handelsvägar + förseningsrisk (väder & andra störningsfaktorer)

De 10 stora handelsvägarna (waypoints + TEU-andel + via-chokepoints, kurerade
i atlas-earth) ritas på globen, färgade efter **förseningsrisk** = värsta
via-sundets risk. Riskmotorn (`engine/delays.py`) väger fyra signalfamiljer
med läsbara poängbidrag per skäl:

1. **Väder 48 h** (Open-Meteo forecast + marine, nyckelfritt): hårda byar →
   konvoj-/lotsstopp; hög våghöjd → långsammare transiter.
2. **Katastroflarm** (GDACS, nyckelfritt): RÖD cyklon/tsunami ≤800 km eller
   ORANGE ≤400 km från sundet.
3. **Live-kö** (AIS): ankrade fartyg klart över normalt.
4. **Pågående störningsepisod** (PortWatch-motorn), viktad efter allvar —
   ett stängt sund är hög förseningsrisk i sig; omvägens +dygn hämtas ur
   `lanes.json` (Suez → +10–14 d runt Godahoppsudden).

Nivåer: låg < 25 ≤ förhöjd < 55 ≤ hög. Detaljpanelen visar poäng, skäl och
bedömt förseningsintervall; ranking-listan flaggar ⛈ vid förhöjd/hög.

## Drivande & loitrande fartyg → råvarukoppling

Tre observationsfamiljer (`engine/drift.py`, endpoint `/api/drift`):

1. **Egen live-AIS**: NavStatus 2 ("ej under kontroll") / 3 ("begränsad
   manöver") = fartygets egen haveri-deklaration, global; plus "misstänkt
   loitering" (under gång men 0,3–1,5 kn) — enbart inne i chokepoint-zoner,
   utanför är mönstret mest brus.
2. **Global Fishing Watch** (gratis token): riktiga loitering-events och
   omlastningar till havs ur satellit-AIS, 7 dagar — fångar öppet hav.
3. **NGA MSI** (nyckelfritt): officiella "VESSEL ADRIFT/DERELICT/DISABLED"-
   varningar, positioner parsas ur varningstexten.

Råvarukopplingen är mekanistisk och redovisas åt båda hållen: manöverstörda
tankers nära oljesund = utbuds-/incidentrisk (historiskt Brent-positiv
impuls); ≥3 loitrande tankers = flytande lager (kortsiktigt överhäng) —
utom vid skuggflottenav (Malacka/Öresund/Gibraltar) där det är last som
väntar på köpare; omlastningar nära oljesund = sanktionsflödessignal;
manöverstörda lastfartyg = fraktrate-risk (BDRY/BOAT). Allt visas på globen
(magenta/violett/orange markörer) + i sidopanelen "Drivande fartyg → råvaror"
+ per sund i detaljpanelen.

## Hamnlagret (PortWatch) + live-inbound

De ~400 största hamnarna (av PortWatchs 1 400) som klickbara punkter:
7-årsfakta (anlöp/år, andel av landets sjöimport/-export), **dagliga
satellit-AIS-anlöp + import/export-ton (120 d sparkline)**, och rapporterade
hamnstörningar (GDACS-härledda) — som dessutom matas in i förseningsmotorn
(+poäng vid störning ≤300 km från ett sund). Live-paret: AIS-snapshotets
Destination-fält matchas mot aliaslistor (atlas-earths shipping.py-mönster)
→ "på väg hit JUST NU" per storhamn (Rotterdam ~490 fartyg). Golv, inte
totalräkning — bara destinationsdeklarerade fartyg räknas, och det står så.

## Global sjöfartsdensitet (alla kontinenter)

Världsbanken/IMF:s *Global Shipping Traffic Density* (AIS 2015–2021, 500 m-
rutnät, CC BY 4.0) nedskalad till ett 4,6 MB globalt overlay
(`static/shipping_density.png`, byggd ur 9,8 GB GeoTIFF via Zenodo-spegeln).
Visar handelsvägarnas ådror även där terrester live-AIS är blind — Afrika,
Sydamerika, Indiska oceanen, Stilla havet. Toggle i sidopanelen.
Byggskript: engångsjobb med rasterio (overviews i .ovr gör läsningen snabb).

## SAR-lagret: fartyg utan AIS (Sentinel-1)

`scripts`-flödet (scratchpad `sar_run.py`) hämtar senaste Sentinel-1 GRD-scen
via Microsoft Planetary Computer (gratis, nyckelfritt), bygger affin ur
GCP:erna (S1 saknar CRS; svepet är roterat → AOI-hörn projiceras genom
inversen), tröskel 15σ MAD, landmask, → `data/sar_detections.json` →
`/api/sar` → gula punkter på globen. Körtid ~3 s per AOI (COG-overviews).

**Valideringen är berättelsen**: Hormuz-kärnan 2026-09-03 = **1** detektion
(sundet är stängt — radar, PortWatch och AIS säger samma sak oberoende);
Singapores ankringsområde 2026-09-04 = **129** detektioner, ovanpå AIS-
prickarna. Detektorn hittar fartyg där de finns och tomhet där det är tomt.

## Flygfrakt: substitutionskanalen (ADS-B)

Källan kom från en genomgång av **GeoSentinel** (github.com/h9zdev/GeoSentinel),
som pekade ut de nyckelfria ADS-B-community-API:erna. Vi hämtar live flygtrafik
runt varje flaskhals (`adsb.lol`, fallback `adsb.one`; ODbL 1.0) och
klassificerar fraktflyg på operatörs-callsign + skrovtyp.

**Varför det hör hemma i ett sjöfartsprojekt:** flygfrakt är substitutions-
kanalen när sjöfrakten stryps. Uppmätt just nu:

| Sund | Flyg i zonen | Fraktflyg | Sjöfartsläge |
|---|---|---|---|
| Hormuz | 86 | 20 (**23 %**) | stängt sedan mars 2026 |
| Suez | 45 | 8 (**18 %**) | stört sedan dec 2023 |
| Danska sunden | 289 | 5 (**2 %**) | normalt |

Tio gånger högre fraktandel över de stängda sunden än över ett fungerande —
en oberoende bekräftelse på att störningen biter, och ledande indikator för
flygfraktrater. Flaggas `SUBSTITUTION` först när sjöfarten *samtidigt* är
störd; annars sägs det rakt ut att signalen saknas.

*(GeoSentinels egen fartygsdata är samma AISStream vi redan använder — deras
övriga moduler, OSINT/CCTV/dark web, är utanför projektets syfte och har
medvetet inte integrerats.)*

## Flaggstat & skuggflotta (MMSI MID)

MMSI:s tre första siffror är fartygets **flaggstat** (ITU:s MID-allokering) —
en identitet vi tidigare kastade bort. Idén kom från en genomgång av
**FlightAirMap** (github.com/Ysurac/FlightAirMap); tabellen är dock byggd ur
ITU-standarden, inte kopierad därifrån — FlightAirMap är **AGPL-3.0** och
skulle smitta licensen. 289 MID-poster i `data/mmsi_mid.json`.

**Finanssignalen:** sanktionerad olja flyttas av en skuggflotta under små
register vars tankflottor exploderade efter 2022 (Gabon, Kamerun, Cooköarna,
Komorerna, Palau, Barbados m.fl.). Andelen högriskflaggade **tankers** i en
zon, mot ~5 % i den globala tankerflottan, är en observerbar
sanktionsflödesindikator.

Motorn hittade rätt plats helt själv: **Bosporen 18 %** (3 av 17 tankers —
Sierra Leone, Kamerun ×2) mot Gibraltar 2 %, Dover 4 %, Malacka 0 %.
Bosporen är utfarten för rysk och kazakisk olja ur Svarta havet. Signalen
flaggas bara vid oljesund och säger uttryckligen att **flagg är indikation,
inte bevis** — Panama och Liberia flaggar tusentals legitima fartyg.

## Mörka fartyg, omlastningar & militärflyg

Tre härledda signaler ur data vi redan samlar (trösklar från
**thanderoy/ais-tracker**, en maritime-intelligence-backend i Go; logiken är
egen och kör på vårt snapshot i stället för PostgreSQL):

**◌ Mörka fartyg (AIS-gap)** — fartyg som tystnat 6–72 h efter att senast ha
setts i en bevakad zon. Att stänga av AIS är standardmetoden för att dölja
sanktionerad last. >72 h = borta, inte mörkt. Tystnad *utanför* zon räknas
som täckningshål, inte uppsåt. Kräver ~6 h drifttid innan registret fylls.

**⇄ Omlastningar till havs (STS)** — par inom 500 m, båda under 3 knop, i över
30 minuter. Två filter var nödvändiga och hittades genom att granska utfallet:
hamnar ≤25 km utesluts (annars ~6 000 falska träffar bland kajliggare i
Rotterdam) och detektionen begränsas till bevakade zoner (annars pråmar på
Rhen och holländska kanaler). Efter filtren: **3 kandidater, alla tankerpar** —
vid Suez norra ankringsområde och Alboranhavet vid Gibraltar, båda kända
omlastningsområden.

**✈ Militärflyg (≤400 km)** — `adsb.lol/v2/mil` ger militär trafik globalt.
Marinspaning, tankflyg och transportflyg koncentreras där kriser byggs upp,
ofta före officiell rapportering. Uppmätt: amerikanskt tankflyg 146 km från
Hormuz, C-130 vid Suez.

*adsb.lol är en gratis community-tjänst — källan har takthållning på ~1
anrop/sekund. Utan den ströps vi och lagret slocknade tyst.*

## Flygtäckning: 190 → 13 600+

Två förändringar efter en genomgång av community-nätverken:

* **OpenSky Network** (`/api/states/all`, anonym & nyckelfri) ger HELA världens
  luftrum i ett anrop — ~12 000-13 500 flygplan. Källan var redan porterad i
  atlas-earth. Anonym kvot är hård, så svaret cachas 10 min och all filtrering
  sker lokalt.
* **Tre ADS-B-nätverk mergas** i zonfrågorna (adsb.lol + adsb.fi + adsb.one)
  i stället för "första som svarar". Mottagarpopulationerna överlappar bara
  delvis: 103 → 268 flygplan runt sunden. `airplanes.live` svarar 403 för oss.

* **Globalt rutnätssvep**: ~50 punkter à 250 nm över världens trafikerade
  luftrum, svept i bakgrunden var 9:e minut (~50 s per varv med artig
  takthållning). Mätning visade att **~30 % av ADS-B-nätverkens flygplan
  saknas i OpenSky** — svepet bidrar med ~1 300 unika.

Zonfrågorna bär aircraft type och matar substitutionssignalen; OpenSky ger
bredden; rutnätet fyller luckorna. Mergen dedupas på ICAO-hex.

**Totalt på globen: ~42 000 objekt** (28 500 fartyg + 13 700 flygplan),
renderade som primitivsamlingar utan prestandaproblem.

**Fartygssidan är redan vid taket:** AISHub kräver egen mottagare, VT Explorer
och MyShipTracking kräver betald nyckel, BarentsWatch kräver konto. ~28 000
fartyg är vad öppna källor ger.

## Satelliter i omloppsbana + radarpassager (satvis-mönstret)

Konceptet lånat från **Flowm/satvis** (405 stjärnor): satelliter live på en
Cesium-glob med passageprediktion. Vi hämtar banelement från **Celestrak**
(nyckelfritt, cachas 12 h) och propagerar med SGP4.

I stället för hela katalogen väljer vi de satelliter som betyder något här:
**383 satelliter**, varav radarsatelliterna (Sentinel-1, RADARSAT-2) lyfts
fram i gult — det är de som producerar bilderna vårt mörka-fartyg-lager
bygger på.

Den verkliga kopplingen är passageprediktionen: varje sunds panel visar
**när det nästa gång kan avbildas med radar**, oberoende av om fartygen
sänder AIS. "Nästa verifieringstillfälle för att Hormuz faktiskt är tomt är
om 10 h 23 min, RADARSAT-2, 69 km från sundet."

## Radardetektion med storleksskattning (segment-geospatial)

`scripts/sar_detect.py` kör **två detektorer på samma Sentinel-1-scen**,
medvetet kombinerade efter mätning:

| Detektor | Träffar (Singapore) | Tid | Ger |
|---|---|---|---|
| Tröskling (15σ MAD) | 129 | 3 s | antal skrov |
| SAM (`segment-geospatial`, vit_b) | 10 | 84 s | **längd per fartyg** |

Tröskeldetektorn räknar skroven; SAM storleksbestämmer de tydligaste
(uppmätt 120–360 m, realistiskt för ankarplatsen) och längden översätts till
fartygsklass — 360 m = VLCC/ULCV, 200 m = Panamax/Aframax. Det är steget
från "det ligger något där" till "det ligger ett supertankfartyg där", för
fartyg som inte sänder AIS.

Att låta SAM ensam sköta detektionen vore att byta bort 92 % av träffarna
mot storleksinformation — därför körs båda och SAM:s längder fästs på de
tröskeldetektioner de överlappar. Kontrasten består: **Hormuz 5 detektioner,
0 storleksbestämda** (sundet är tomt) mot **Singapore 129 / 10 mätta**.

## Datakällor (historik/priser — nyckelfria)

| Källa | Roll |
|---|---|
| IMF PortWatch (ArcGIS) | Dagliga chokepoint-passager + DWT ur satellit-AIS, 2019→, ~5 dygns lag |
| Yahoo Finance chart-API | Citat + dagshistorik för kopplade instrument (BZ=F, ZW=F, BDRY, SMH …) |
| Esri World Imagery | Globens baskarta |

## Arkitektur

```
atlas_choke/
  config.py            miljödriven konfig (slimmad ur atlas-earth)
  cache.py             cache-motor: SWR, circuit breaker (ÅTERANVÄND från atlas-earth)
  ais_collector.py     AIS-insamlare i egen process (ÅTERANVÄND)
  sources/
    base.py            källbasklass, tolerant TLS (ÅTERANVÄND)
    aisstream.py       live-AIS via egen WebSocket-klient; korridor-/globalläge (ÅTERANVÄND+utbyggd)
    digitraffic.py     finsk nationell AIS, Östersjön (nyckelfri)  ← NY
    barentswatch.py    norsk AIS, EEZ+Svalbard (valfritt gratis konto)  ← NY
    portwatch.py       chokepoint-serier, paginerad ArcGIS (slimmad)
    markets.py         Yahoo-citat + historik (slimmad)
  engine/
    stress.py          stressindex med frusen baslinje   ← NY
    events.py          event-studie: episoder → forward returns  ← NY
    forecast.py        analogband vs basnivå, edge, konfidens    ← NY
    live.py            live-zonstatistik, tonnage-proxy, passageräknare, egen historik  ← NY
  server/
    app.py             FastAPI: /api/overview, /api/chokepoint/{id},
                       /api/markets, /api/vessels, /api/health
    static/            Cesium-glob + boot-sekvens (atlas-estetik), ny app-logik
  data/chokepoints.json  11 sund: PortWatch-namn, koordinater,
                         kopplade instrument + transmissionslogik
```

Ursprungsprojektet `atlas-earth` är orört — allt återbruk är kopior.

## Tre demo-lägen: dåtid, framtid, nuläge

**▶ TIDSMASKIN** (topbar) — spelar upp 2019 → idag på globen, en vecka per
110 ms, ur veckosamplad riktad z (`z_weekly` i bundeln). Historiens kriser
tänds i tur ordning med etikett: COVID-19, Ever Given, Ukraina/Bosporen,
Panama-torkan, Röda havet, Hormuz. Lederna döljs under uppspelning (de bär
nutidens risk). Öppningsscenen: 7 års världshandel på 40 sekunder.

**Scenario** (sidopanel) — "vad händer om X stängs?". Sundet tvingas till full
stress och **samma motorer som live-analysen** räknar om risk, förseningar och
leder; omdirigeringsmottagaren (Godahoppsudden) belastas när Suez/Bab-el-Mandeb
stängs. Prisutfallen är sundets **faktiska historiska analoger**, inte påhitt.
Taiwansundet stängt → 37 % av global container-TEU på berörda leder, SMH −1,5 %,
TSM −1,2 % (N=5). Allt märkt SIMULERING.

**IDAG** (topbar) — automatgenererad lägesrapport ur all live-data: stressade
flaskhalsar, förseningsrisker med dygn, påverkade handelsvägar med TEU-andel,
största marknadsrörelser, live-köer och radardetektioner. Deterministisk
mallogik (ingen LLM) — demosäker.

## Demo-manus (2 min)

1. **Boot-skärmen** → globen: tre röda noder i Mellanöstern, blå streckade
   bågar ner mot Godahoppsudden. "Det där är Röda havet-krisen och
   Hormuz-stängningen, direkt ur satellitdata."
2. Klicka **Hormuzsundet**: 81/100, tonnage −98 %, pågående dag 180+,
   episodlistan visar att motorn själv hittade stängningen 2026-03-04.
3. Scrolla till **prisscenarierna**: "Efter liknande episodstarter har Brent
   gjort +1,7 % på 5 dagar, +5,8 % på 20 — bandet är brett och N=7, och det
   står där. Vi hittar inte på precision som inte finns."
4. Klicka **Godahoppsudden**: invers nod, +80 % trafik — beviset för att
   omdirigeringen är verklig, inte en datastörning.
5. Avsluta med tickern: Brent +6 %, torrlastfrakt +7 % denna vecka.
   "Signalen fanns i satellitdatan först."

## Tekniker lånade från OSS-ekosystemet (GitHub-skanning 2026-09-05)

- **Dead reckoning + primitiv-rendering**: PlutonusDev/worldview + deck.gl-
  diskussion #10260 (mönster, ej kod — demo-licenser). `PointPrimitiveCollection`
  klarar 20k+ punkter där entity-vägen kvävs vid ett par tusen.
- **Multi-käll-merge per MMSI**: dma-ais/AisTrack (Apache-2.0) och tormol/AIS
  (mönstret: färskaste källan vinner per fartyg, staleness-eviction).
- **Kö-/väntetrösklar**: GlobalFishingWatch/anchorages_pipeline (Apache-2.0):
  ankrad via NavigationalStatus + fartgräns; pixel-ports/pixel_ais:
  navstatus-utjämning.
- **Transit-räkning**: hormuz.now-metodiken (bbox-inträden, fart > 0,5 kn).
- **Last-proxy**: IMF WP/19/275 via Världsbankens pacific-observatory
  (deplacement = L×B×d×ρ×c_b).
- **PortWatch-anomalier som precedens**: amanid/imf-portwatch-analytics (MIT).
- Källäget 2026: aisstream.io är i praktiken oövervakat (upprepade 2026-avbrott
  utan svar); Digitraffic och BarentsWatch är de stabila öppna feedsen;
  AISHub kräver egen mottagare; GFW:s gratisnivå har inga live-positioner.

## Ärlighetsdeklaration

- PortWatch släpar ~5 dygn (satellitbearbetning) — detta är en **lednings-
  indikator mot nyhetscykeln och prisgenomslag i realekonomin**, inte mot
  intradag-marknaden.
- Prognosbanden är empiriska analoger med litet N (stora störningar är
  sällsynta). De visar riktning + historisk spridning, inte alfa.
- Live-AIS-lagret är avstängt när AISStream ligger nere; ingen simulerad
  fartygsdata visas någonsin som live.
