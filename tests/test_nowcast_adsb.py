"""Dumpnavigering på en syntetisk tar i minnet — inga nätverksanrop."""

from __future__ import annotations

import io
import tarfile

import pytest

from cjt_nowcast import adsb


def _tar(prefix_members: list[tuple[str, int]], n_traces: int, trace_size: int) -> tuple[bytes, int]:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as t:
        for name, size in prefix_members + [(f"./traces/{i % 256:02x}/trace_full_c0{i:04x}.json", trace_size)
                                            for i in range(n_traces)]:
            info = tarfile.TarInfo(name)
            info.size = size
            t.addfile(info, io.BytesIO(b"\x01" * size))
    data = buf.getvalue()
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        first = next(m.offset for m in t.getmembers() if "/traces/" in m.name)
    return data, first


def _reader(data: bytes):
    return lambda off, n: data[off:off + n]


@pytest.mark.parametrize("prefix", [
    [("./heatmap/00.bin.ttf", 900_000), ("./heatmap/01.bin.ttf", 700_000)],             # få stora
    [(f"./acas/{i}.json", 3_000) for i in range(600)],                                     # många små
    [("./heatmap/00.bin.ttf", 1_500_000)] + [(f"./acas/{i}.json", 700) for i in range(300)],
])
def test_traces_start_finds_exact_first_trace_header(prefix):
    data, first = _tar(prefix, n_traces=400, trace_size=9_000)
    assert adsb.traces_start(_reader(data), len(data)) == first


def test_stream_from_offset_yields_all_traces():
    data, first = _tar([("./heatmap/00.bin.ttf", 1_200_000)], n_traces=300, trace_size=7_000)
    off = adsb.traces_start(_reader(data), len(data))
    with tarfile.open(fileobj=io.BytesIO(data[off:]), mode="r|") as t:
        names = [m.name for m in t]
    assert len(names) == 300 and all("/traces/" in n for n in names)


def test_unknown_layout_falls_back_to_full_stream():
    data, _ = _tar([], n_traces=300, trace_size=7_000)       # börjar direkt med traces/
    assert adsb.traces_start(_reader(data), len(data)) == 0
