"""FastAPI-app för omsättnings-UI:t. Egen port (standard 8061) — Atlas-trackern kör på 8060.

    python -m cjt_nowcast ui            # → http://127.0.0.1:8061
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import data

LOG = logging.getLogger("cjt.ui")
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="CJT Revenue Intelligence", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_state: dict = {"payload": None, "built_at": None, "error": None}
_lock = threading.Lock()


def _build() -> None:
    t0 = time.monotonic()
    try:
        p = data.payload()
        with _lock:
            _state.update(payload=p, built_at=time.strftime("%Y-%m-%d %H:%M:%S"), error=None)
        LOG.info("UI-data byggd på %.1f s", time.monotonic() - t0)
    except Exception as exc:  # noqa: BLE001 — UI:t ska visa felet, inte krascha
        LOG.exception("UI-data kunde inte byggas")
        with _lock:
            _state["error"] = repr(exc)


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_build, daemon=True).start()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/all")
def api_all() -> JSONResponse:
    with _lock:
        if _state["payload"] is None:
            return JSONResponse({"status": "building", "error": _state["error"]}, status_code=202)
        return JSONResponse({"status": "ok", "built_at": _state["built_at"], **_state["payload"]})


@app.get("/api/daily")
def api_daily() -> list[dict]:
    # dagsfilen växer medan insamlaren kör — läs alltid färskt
    return data.daily()


@app.post("/api/refresh")
def api_refresh() -> dict:
    threading.Thread(target=_build, daemon=True).start()
    return {"status": "rebuilding"}


def main(host: str = "127.0.0.1", port: int = 8061) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
