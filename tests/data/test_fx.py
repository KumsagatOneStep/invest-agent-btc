import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import respx

from invest_agent.data.fx import ensure_schema, usd_kzt

URL = "https://open.er-api.com/v6/latest/USD"
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    return c


@respx.mock
def test_fetches_and_caches_for_a_day():
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"rates": {"KZT": 510.5}}))
    c, http = conn(), httpx.Client()
    assert usd_kzt(NOW, c, http) == 510.5
    assert usd_kzt(NOW + timedelta(hours=23), c, http) == 510.5
    assert route.call_count == 1
    respx.get(URL).mock(return_value=httpx.Response(200, json={"rates": {"KZT": 520.0}}))
    assert usd_kzt(NOW + timedelta(hours=25), c, http) == 520.0


@respx.mock
def test_fx_falls_back_to_cached_rate():
    c, http = conn(), httpx.Client()
    respx.get(URL).mock(return_value=httpx.Response(200, json={"rates": {"KZT": 510.5}}))
    usd_kzt(NOW, c, http)
    respx.get(URL).mock(return_value=httpx.Response(500))
    assert usd_kzt(NOW + timedelta(days=3), c, http) == 510.5
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    assert usd_kzt(NOW + timedelta(days=3), c, http) == 510.5


@respx.mock
def test_fx_none_without_cache():
    respx.get(URL).mock(return_value=httpx.Response(500))
    assert usd_kzt(NOW, conn(), httpx.Client()) is None


@respx.mock
def test_fx_rejects_non_positive_rate():
    respx.get(URL).mock(return_value=httpx.Response(200, json={"rates": {"KZT": 0}}))
    assert usd_kzt(NOW, conn(), httpx.Client()) is None
    c, http = conn(), httpx.Client()
    respx.get(URL).mock(return_value=httpx.Response(200, json={"rates": {"KZT": 510.5}}))
    usd_kzt(NOW, c, http)
    respx.get(URL).mock(return_value=httpx.Response(200, json={"rates": {"KZT": -1}}))
    assert usd_kzt(NOW + timedelta(days=2), c, http) == 510.5
