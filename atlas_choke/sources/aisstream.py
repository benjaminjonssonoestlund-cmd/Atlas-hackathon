"""AISStream — live fartygs-AIS över WebSocket (gratis nyckel).

Pythons standardbibliotek saknar WebSocket-klient, så vi implementerar en minimal
egen över socket+ssl (handshake + maskade textframes). En daemon-tråd håller
strömmen öppen, prenumererar på boxar runt sjöfartens flaskhalsar och lagrar
senaste positionen per fartyg. Trängsel vid choke points blir en direkt
handelssignal.

Sätt AISSTREAM_API_KEY (aisstream.io). Utan nyckel är lagret tomt.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import ssl
import struct
from pathlib import Path

from ..config import CONFIG
from .base import DataSource

log = logging.getLogger("atlas.aisstream")

WS_HOST = "stream.aisstream.io"
WS_PATH = "/v0/stream"

# Täta boxar runt strategiska flaskhalsar + världens ~35 travlaste hamnar.
# Läge "chokepoints" — lägst meddelandetakt, för svag hårdvara/molngratis.
PORT_BOXES = [
    # --- Strategiska flaskhalsar ---
    [[24.0, 54.0], [28.0, 58.0]],        # Hormuz
    [[27.0, 32.0], [33.0, 35.0]],        # Suez / norra Röda havet
    [[11.0, 42.0], [15.0, 45.0]],        # Bab-el-Mandeb
    [[0.0, 99.0], [6.0, 105.0]],         # Malacka
    [[7.0, -81.0], [11.0, -78.0]],       # Panama
    [[40.0, 27.0], [42.0, 30.0]],        # Bosporen
    [[35.0, -6.5], [37.0, -5.0]],        # Gibraltar
    [[50.0, 0.5], [52.0, 2.5]],          # Engelska kanalen (Dover)
    [[54.5, 10.0], [57.5, 13.5]],        # Danska sunden
    [[22.0, 118.0], [26.0, 121.5]],      # Taiwansundet
    [[30.4, 121.3], [31.6, 122.9]],      # Shanghai / Yangshan
    [[22.3, 113.6], [22.85, 114.4]],     # Shenzhen / Hongkong
    [[1.1, 103.6], [1.45, 104.05]],      # Singapore
    [[51.7, 3.4], [52.2, 4.5]],          # Rotterdam / Europoort
]

# Läge "corridors" (STANDARD lokalt): stora regioner längs världens farleder —
# fartygen syns längs HELA rutten, inte bara vid nålsögonen. Kurerad i
# atlas-earth (ais.py REGIONS) och beprövad där. Kräver mer CPU/RAM än
# gratis-moln (Render 512 MB OOM:ar) men är oproblematisk på en arbetsstation.
# aisstream: [[lat_min, lon_min], [lat_max, lon_max]]
CORRIDOR_BOXES = [
    [[-12.0, 92.0], [35.0, 145.0]],     # Sydostasien + Malacka + Taiwan + Sydkinesiska sjön
    [[20.0, 118.0], [42.0, 145.0]],     # Östkina/Japan/Korea
    [[10.0, 40.0], [32.0, 62.0]],       # Persiska viken + Hormuz + Arabiska havet
    [[8.0, 30.0], [35.0, 48.0]],        # Röda havet + Bab-el-Mandeb + Suez
    [[30.0, -12.0], [62.0, 32.0]],      # Europa: Medelhavet–Nordsjön–Östersjön
    [[5.0, -100.0], [33.0, -55.0]],     # Karibien + Panama + US Gulf
    [[22.0, -130.0], [50.0, -112.0]],   # USA västkust
    [[30.0, -82.0], [46.0, -60.0]],     # USA östkust
    [[-40.0, 10.0], [-25.0, 35.0]],     # Godahoppsudden
    [[-40.0, -75.0], [5.0, -30.0]],     # Sydamerika östkust (Santos m.fl.)
    [[-45.0, 108.0], [-8.0, 155.0]],    # Australien (bulk/LNG)
    [[0.0, 62.0], [25.0, 95.0]],        # Indien + Bengaliska viken
    [[-10.0, -20.0], [20.0, 12.0]],     # Västafrika/Guineabukten
    [[-35.0, 25.0], [12.0, 55.0]],      # Östafrika + Moçambiquekanalen
]
# TÄCKNINGSVERKLIGHET (ur atlas-earth): aisstream är TERRESTER AIS — tätt i
# Europa/USA/Japan/Korea/Singapore, glest i Afrika/Mellanöstern, och Kina
# stryper AIS-delning sedan 2021. Fler rutor hjälper bara där mottagare finns.

# ATLAS_AIS_MODE: chokepoints | corridors (standard) | global
_MODE = os.environ.get("ATLAS_AIS_MODE", "corridors").strip().lower()
if os.environ.get("ATLAS_AIS_GLOBAL", "0") == "1" or _MODE == "global":
    BOUNDING_BOXES = [[[-90.0, -180.0], [90.0, 180.0]]]
elif _MODE == "chokepoints":
    BOUNDING_BOXES = PORT_BOXES
else:
    BOUNDING_BOXES = CORRIDOR_BOXES

# Tak för hur många fartyg snapshotet returnerar (env-justerbart).
MAX_VESSELS = int(os.environ.get("ATLAS_AIS_MAX", "25000"))

# Insamlaren skriver en färdig JSON-snapshot hit; webbservern läser bara den.
SNAPSHOT_PATH = Path(os.environ.get("ATLAS_AIS_SNAPSHOT")
                     or (Path(__file__).resolve().parent.parent / "data" / "ais_snapshot.json"))

# AIS-typkod → (kategori, färg) — matchar fartygslegenden i UI:t
def _category(type_code: int | None) -> tuple[str, str]:
    t = type_code or 0
    if 80 <= t <= 89:
        return "tanker", "#ff9b3d"
    if 70 <= t <= 79:
        return "lastfartyg", "#2aff9e"
    if 60 <= t <= 69:
        return "passagerare", "#66b6ff"
    if t == 30:
        return "fiske", "#b46bff"
    if 50 <= t <= 59:
        return "special", "#b46bff"
    return "okänt", "#c0c8d0"

# Glöm fartyg som inte hörts av på så här länge. 25 min är standard; i globalt
# läge lyfter ett längre fönster (t.ex. 3600 s) totalantalet markant — äldre
# positioner extrapoleras ändå av dead reckoning i UI:t.
STALE_SECONDS = int(os.environ.get("ATLAS_AIS_STALE", "1500"))


def _ws_connect(timeout: float = 30.0):
    # WebSocketen bygger sin EGEN SSL-kontext och omfattas därför inte av den
    # globala urllib-fallbacken i config.py. Utan detta dör AIS på maskiner vars
    # CA-lager avvisas av strikt OpenSSL ("Basic Constraints ... not critical") —
    # samma avvägning som base.py: publik läsdata, ingen autentisering skickas.
    # ATLAS_STRICT_TLS=1 (satt i molnet) behåller strikt validering.
    raw = socket.create_connection((WS_HOST, 443), timeout=timeout)
    try:
        ctx = ssl.create_default_context()
        s = ctx.wrap_socket(raw, server_hostname=WS_HOST)
    except ssl.SSLCertVerificationError:
        if os.environ.get("ATLAS_STRICT_TLS") == "1":
            raise
        try:
            raw.close()
        except OSError:
            pass
        raw = socket.create_connection((WS_HOST, 443), timeout=timeout)
        s = ssl._create_unverified_context().wrap_socket(raw, server_hostname=WS_HOST)
    ws_key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {WS_PATH} HTTP/1.1\r\nHost: {WS_HOST}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {ws_key}\r\n"
           f"Sec-WebSocket-Version: 13\r\n\r\n")
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise ConnectionError("handshake stängdes")
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise ConnectionError("handshake misslyckades: " + buf[:80].decode("latin1"))
    return s


def _ws_frame(s, opcode: int, payload: bytes) -> None:
    """Skicka EN maskerad WebSocket-ram. Klienter MÅSTE maskera (RFC 6455)."""
    header = bytearray([0x80 | (opcode & 0x0F)])
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(header) + masked)


def _ws_send_pong(s, payload: bytes = b"") -> None:
    """Svara på serverns PING.

    Utan detta stänger aisstream anslutningen efter sin keepalive-timeout —
    i praktiken exakt var 93:e sekund, oavsett om data flödar eller inte.
    Insamlaren loopade därför i evighet: anslut, tyst i halvannan minut,
    utsparkad, backoff, anslut igen. Snapshotet frös i 22 timmar utan att
    något felade synligt, eftersom varje enskild återanslutning såg ut att
    lyckas.

    RFC 6455 §5.5.3: en PONG ska bära PING-ramens exakta payload.
    """
    _ws_frame(s, 0xA, payload)


def _ws_send_text(s, text: str) -> None:
    payload = text.encode("utf-8")
    header = bytearray([0x81])
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    s.sendall(bytes(header) + masked)


def _ws_read_frame(s):
    def recvn(n):
        d = b""
        while len(d) < n:
            chunk = s.recv(n - len(d))
            if not chunk:
                raise ConnectionError("closed")
            d += chunk
        return d
    b0, b1 = recvn(2)
    opcode = b0 & 0x0F
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", recvn(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", recvn(8))[0]
    payload = recvn(ln) if ln else b""
    return opcode, payload


class AisStreamSource(DataSource):
    """Läsare för live-AIS. Själva insamlingen sker i en SEPARAT PROCESS
    (ais_collector) med egen GIL, som skriver en färdig JSON-snapshot till fil.
    Denna klass startar processen och läser bara filen → webbservern kan aldrig
    svältas ut av den CPU-tunga parsningen, ens vid 15-20k+ fartyg globalt."""

    name = "aisstream"
    description = "Live fartygs-AIS (AISStream) — insamling i egen process"

    def __init__(self):
        self._proc = None
        self._started = False
        self._cache_mtime = -1.0
        self._cache_items: list[dict] = []

    @property
    def available(self) -> bool:
        return bool(CONFIG.aisstream_api_key)

    def ensure_started(self) -> None:
        if self._started or not self.available:
            return
        self._started = True
        import atexit
        import subprocess
        import sys
        # Kör från repo-roten (mappen som innehåller paketet atlas_choke) så att
        # "-m atlas_choke.ais_collector" hittas. Ärver env → nyckel + TLS-flagga.
        root = str(Path(__file__).resolve().parent.parent.parent)
        try:
            # stderr ärvs (None) → insamlarens loggrader syns i Render-loggen,
            # så man kan diagnosticera anslutning/fel. stdout kastas.
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "atlas_choke.ais_collector"],
                cwd=root, stdout=subprocess.DEVNULL, stderr=None)
            atexit.register(self._stop)
            log.info("aisstream: insamlare startad i egen process (pid %s)", self._proc.pid)
        except Exception as exc:  # noqa: BLE001 — utan insamlare är lagret bara tomt
            log.warning("aisstream: kunde inte starta insamlaren: %s", exc)

    def _stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def _read(self) -> list[dict]:
        # Läs bara om filen om den ändrats (mtime) — annars servera cachen.
        try:
            mtime = SNAPSHOT_PATH.stat().st_mtime
        except OSError:
            return self._cache_items
        if mtime != self._cache_mtime:
            try:
                data = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
                self._cache_items = data.get("items", [])
                self._cache_mtime = mtime
            except (ValueError, OSError):
                pass
        return self._cache_items

    def snapshot(self, limit: int = MAX_VESSELS) -> list[dict]:
        self.ensure_started()
        items = self._read()
        return items[:limit] if limit else items

    def raw_snapshot(self) -> bytes | None:
        """Rå JSON-bytes direkt från insamlarens fil (redan i API-svarsform).
        Serveras oförändrat → ingen dyr om-serialisering av 16k+ fartyg."""
        self.ensure_started()
        try:
            return SNAPSHOT_PATH.read_bytes()
        except OSError:
            return None

    def check(self) -> bool:
        return self.available
