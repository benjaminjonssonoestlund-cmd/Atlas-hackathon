@echo off
REM ============================================================================
REM AIS-catcher-station för Atlas Chokepoint — nyckelfärdig start.
REM
REM 1) Ladda ner Windows-versionen: github.com/jvde-github/AIS-catcher/releases
REM    (zip:en "AIS-catcher ... Windows x64") och packa upp i denna mapp.
REM 2) Koppla in en RTL-SDR-dongel (~300-500 kr) + VHF-antenn nära fönster/tak.
REM 3) Registrera stationen på aiscatcher.org -> klistra in din SHARING KEY nedan
REM    (delning låser upp community-feeden i stationens UI - reciprocitet).
REM 4) Kör denna fil. Lägg sedan i atlas-projektets .env:
REM       ATLAS_AISCATCHER_URL=http://127.0.0.1:8100
REM    så mergas dina egna mottagna fartyg in i Atlas live-bild automatiskt.
REM
REM Flaggorna:
REM   -d:0            första RTL-SDR-enheten
REM   -N 8100         inbyggd webserver + JSON-API på port 8100
REM   -X <KEY>        dela till aiscatcher.org (byt mot din nyckel)
REM   -x 127.0.0.1 10110  lyssna även på rå NMEA via UDP (andra verktyg kan mata in)
REM ============================================================================

set SHARING_KEY=DIN_SHARING_KEY_HAR

AIS-catcher.exe -d:0 -N 8100 -X %SHARING_KEY% -x 127.0.0.1 10110
pause
