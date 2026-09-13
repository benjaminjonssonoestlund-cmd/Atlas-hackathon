"""Flygplatsuppslag: närmaste flygplats till en markposition.

Källa: OurAirports (public domain), cachad i raw/. Vi behåller stora och
medelstora flygplatser plus alla med IATA-kod — Cargojet trafikerar även
mindre fält (YQM, YHM) som OurAirports klassar som medium/small men som
alltid har IATA-kod. Helikopterplattor, sjöflyg och nedlagda fält faller bort
redan här så att en markpunkt vid YYZ aldrig matchas mot ett sjukhustak.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd

from .common import RAW, fetch_cached

URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
KEEP_TYPES = {"large_airport", "medium_airport"}
EARTH_KM = 6371.0


@dataclass(frozen=True)
class Airport:
    code: str        # IATA om den finns, annars ICAO/ident
    icao: str
    name: str
    country: str     # ISO-3166 alfa-2
    lat: float
    lon: float
    km: float        # avstånd från frågepunkten


def load(refresh: bool = False) -> pd.DataFrame:
    path = fetch_cached(URL, RAW / "ourairports" / "airports.csv", refresh=refresh)
    df = pd.read_csv(path, keep_default_na=False,
                     usecols=["ident", "type", "name", "latitude_deg", "longitude_deg",
                              "elevation_ft", "continent", "iso_country", "iata_code", "gps_code"])
    df = df[df["type"].isin(KEEP_TYPES) | ((df["iata_code"] != "") & (df["type"] == "small_airport"))]
    # OurAirports har dubblettposter ovanpå riktiga fält, t.ex. "CA-1291" = "(Duplicate)YEG"
    df = df[~df["name"].str.startswith("(Duplicate)")]
    df = df.assign(code=np.where(df["iata_code"] != "", df["iata_code"], df["ident"]),
                   icao=np.where(df["gps_code"] != "", df["gps_code"], df["ident"]),
                   iata=df["iata_code"] != "",
                   # höjd behövs för "lågt nära fältet" — Calgary ligger på 3 600 ft
                   elev_ft=pd.to_numeric(df["elevation_ft"], errors="coerce").fillna(0.0))
    return df.rename(columns={"latitude_deg": "lat", "longitude_deg": "lon",
                              "iso_country": "country"})[
        ["code", "icao", "iata", "name", "country", "continent", "type", "lat", "lon", "elev_ft"]
    ].reset_index(drop=True)


class AirportIndex:
    """Vektoriserad närmaste-granne på haversine. ~8k fält → snabbt nog utan KD-träd."""

    def __init__(self, df: pd.DataFrame):
        self.df = df.reset_index(drop=True)
        self._lat = np.radians(self.df["lat"].to_numpy(float))
        self._lon = np.radians(self.df["lon"].to_numpy(float))

    def nearest(self, lat: float, lon: float, max_km: float = 8.0) -> Airport | None:
        la, lo = np.radians(lat), np.radians(lon)
        a = (np.sin((self._lat - la) / 2) ** 2
             + np.cos(la) * np.cos(self._lat) * np.sin((self._lon - lo) / 2) ** 2)
        km = 2 * EARTH_KM * np.arcsin(np.sqrt(a))
        i = int(np.argmin(km))
        if km[i] > max_km:
            return None
        r = self.df.iloc[i]
        return Airport(r["code"], r["icao"], r["name"], r["country"],
                       float(r["lat"]), float(r["lon"]), float(km[i]))


@lru_cache(maxsize=1)
def index() -> AirportIndex:
    return AirportIndex(load())
