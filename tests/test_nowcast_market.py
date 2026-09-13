"""Stooq-tolkning och nyckelmaskering — inga nätverksanrop."""

from __future__ import annotations

import pytest

from cjt_nowcast.common import redact
from cjt_nowcast.market import PriceSourceError, parse_price_csv, parse_stooq_csv


def test_parse_investing_com_export():
    text = ('"Date","Price","Open","High","Low","Vol.","Change %"\n'
            '"08/11/2026","103.80","101.50","104.00","100.20","120.00K","4.22%"\n'
            '"08/10/2026","99.60","99.00","100.10","98.50","90.00K","-0.40%"\n')
    df = parse_price_csv(text)
    assert list(df["close"]) == [99.6, 103.8] and str(df["date"].iloc[-1]) == "2026-08-11"


def test_parse_yahoo_export():
    text = "Date,Open,High,Low,Close,Adj Close,Volume\n2026-08-10,99.0,100.1,98.5,99.6,98.9,90000\n"
    df = parse_price_csv(text)
    assert df["close"].iloc[0] == 99.6                 # ojusterad stängning


def test_unknown_price_format_raises():
    with pytest.raises(PriceSourceError):
        parse_price_csv("foo,bar\n1,2\n")


def test_parse_stooq_csv():
    text = "Date,Open,High,Low,Close,Volume\n20260811,101.5,104.0,100.2,103.8,120000\n20260810,99.0,100.1,98.5,99.6,90000\n"
    df = parse_stooq_csv(text)
    assert list(df["close"]) == [99.6, 103.8]          # sorterat på datum
    assert str(df["date"].iloc[0]) == "2026-08-10"


@pytest.mark.parametrize("body", [
    "<html><body>This site requires JavaScript to verify your browser.</body></html>",
    "Daily request limit exceeded",
    "Date,Open,High,Low,Close,Volume\n",
])
def test_non_price_responses_raise(body):
    with pytest.raises(PriceSourceError):
        parse_stooq_csv(body)


def test_api_key_is_redacted_in_logged_urls():
    url = "https://stooq.com/q/d/l/?s=x&d1=20150101&i=d&apikey=SECRET123"
    assert "SECRET123" not in redact(url) and redact(url).endswith("apikey=<redacted>")
