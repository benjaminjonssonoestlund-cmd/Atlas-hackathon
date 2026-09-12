"""Miljödriven konfiguration för Atlas Chokepoint (hackathon-bygget).

Slimmad ur atlas-earth: bara det som chokepoint-monitorn behöver.
Allt fungerar utan nycklar — AISStream-nyckeln är valfri bonus för
live-fartygslagret (deras ström har dessutom legat tyst sedan aug 2026,
så kärnan bygger på IMF PortWatch som är helt nyckelfri).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env-laddare (stdlib): KEY=VALUE-rader ur projektroten.

    Redan satta miljövariabler vinner — .env är bekvämlighet, inte auktoritet.
    Körs FÖRE Config instansieras så att t.ex. BARENTSWATCH_* plockas upp.
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    # Server
    host: str = field(default_factory=lambda: _env("ATLAS_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(_env("ATLAS_PORT", "8060")))

    # Cache-TTL (sekunder)
    ttl_fast: int = field(default_factory=lambda: int(_env("ATLAS_TTL_FAST", "60")))
    ttl_medium: int = field(default_factory=lambda: int(_env("ATLAS_TTL_MEDIUM", "600")))
    ttl_slow: int = field(default_factory=lambda: int(_env("ATLAS_TTL_SLOW", "86400")))

    # HTTP
    http_timeout: float = field(default_factory=lambda: float(_env("ATLAS_HTTP_TIMEOUT", "20")))
    user_agent: str = field(
        default_factory=lambda: _env("ATLAS_USER_AGENT",
                                     "atlas-chokepoint/0.1 (open-data hackathon)")
    )

    # Hur långt bak PortWatch-historiken hämtas (träning/event-study).
    # 2019-01-01 = hela PortWatch-historiken: täcker COVID-kollapsen (2020),
    # Ever Given (mars 2021), Panama-torkan (2023), Röda havet-krisen
    # (2023-24) och Hormuz-stängningen (2026) — alla naturliga experiment.
    portwatch_since: str = field(default_factory=lambda: _env("ATLAS_PW_SINCE", "2019-01-01"))

    # Nycklar (valfria)
    cesium_ion_token: str = field(default_factory=lambda: _env("CESIUM_ION_TOKEN", ""))
    aisstream_api_key: str = field(default_factory=lambda: _env("AISSTREAM_API_KEY", "").strip())


CONFIG = Config()


def _install_tls_fallback() -> None:
    """Tolerant TLS för utgående hämtningar — ärvt medvetet val från atlas-earth.

    Maskinens CA-lager avvisar vissa mellanliggande CA:n med strikt OpenSSL 3.x
    ("Basic Constraints ... not marked critical"), vilket tyst tystar helt
    orelaterade publika källor. Vi hämtar uteslutande publik, anonym läsdata
    och skickar aldrig hemligheter — hellre fungerande insamling än trasig
    validering. Sätt ATLAS_STRICT_TLS=1 för strikt läge.
    """
    if _env("ATLAS_STRICT_TLS", "") == "1":
        return
    import ssl
    try:
        ssl._create_default_https_context = ssl._create_unverified_context
    except Exception:  # noqa: BLE001
        pass


_install_tls_fallback()
