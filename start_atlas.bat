@echo off
REM Atlas Chokepoint — rekommenderad start (globalt AIS-läge, 3 h-fönster).
REM Nycklar/valfria källor läses ur .env (kopiera .env.example -> .env).
cd /d "%~dp0"
set ATLAS_AIS_MODE=global
set ATLAS_AIS_MAX=200000
set ATLAS_AIS_WRITE_EVERY=12
set ATLAS_AIS_STALE=10800
set ATLAS_AIS_BACKOFF=45
python run.py
pause
