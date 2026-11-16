import json

import pytest

import smart_trader_pro.context as context
from smart_trader_pro import alpha, cli
from smart_trader_pro.data import MarketData
from conftest import HOUR
from test_breakout import T0, flat_btc, quiet_then_breakout
from test_io import FakeResponse


def token(aid, sym, vol="900000", liq="300000", **over):
    t = {"alphaId": aid, "symbol": sym, "name": sym.title(), "chainName": "BSC", "price": "0.5",
         "percentChange24h": "12.5", "volume24h": vol, "liquidity": liq, "marketCap": "8000000",
         "holders": "7120", "offline": False, "stockState": False, "offsell": False,
         "listingCex": False}
    t.update(over)
    return t


def wrap(data, success=True):
    return FakeResponse({"code": "000000", "success": success, "data": data})


def test_tokens_are_filtered_and_sorted(monkeypatch):
    d = alpha.AlphaData(pause=0)
    listing = [token("ALPHA_1", "gem", vol="500000"), token("ALPHA_2", "big", vol="2000000"),
               token("ALPHA_3", "thin", liq="1000"), token("ALPHA_4", "quiet", vol="50"),
               token("ALPHA_5", "gone", offline=True), token("ALPHA_6", "stock", stockState=True),
               {"symbol": "noid"}, "pas un jeton"]
    monkeypatch.setattr(d.session, "get", lambda url, params=None, timeout=None: wrap(listing))
    got = d.tokens()
    assert [t["id"] for t in got] == ["ALPHA_2", "ALPHA_1"]
    assert got[0]["symbol"] == "BIG" and got[0]["holders"] == 7120 and got[0]["change_24h"] == 12.5
    assert set(d.tokens_by_id) == {"ALPHA_1", "ALPHA_2"}
    assert alpha.info_lines(got[0])[0] == "Big sur BSC"
    assert "8.0 M $" in alpha.info_lines(got[0])[1]


def test_unavailable_api_raises(monkeypatch):
    d = alpha.AlphaData(pause=0)
    monkeypatch.setattr(d.session, "get", lambda url, params=None, timeout=None: FakeResponse({}, 451))
    with pytest.raises(RuntimeError):
        d.tokens()
    monkeypatch.setattr(d.session, "get", lambda url, params=None, timeout=None: wrap([], success=False))
    with pytest.raises(RuntimeError):
        d.tokens()


def test_klines_parse_strings_and_drop_open_candle(monkeypatch):
    d = alpha.AlphaData(pause=0)
    d.tokens_by_id = {"ALPHA_9": {"symbol": "GEM"}}
    rows = [[str(T0 + i * HOUR), "1.0", "1.2", "0.9", "1.1", "100", str(T0 + (i + 1) * HOUR - 1),
             "110", "5", "70", "77", "0"] for i in range(6)]
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(params)
        return wrap(list(reversed(rows)) + [["abime"]])

    monkeypatch.setattr(d.session, "get", fake_get)
    c = d.klines("ALPHA_9", "1h", total=4, now_ms=T0 + 5 * HOUR + 10)    # la 6e bougie est ouverte
    assert seen["symbol"] == "ALPHA_9USDT" and seen["interval"] == "1h"
    assert c.symbol == "GEMUSDT" and len(c) == 4
    assert c.ts == [T0 + i * HOUR for i in range(1, 5)] and c.taker_buy == [70.0] * 4
    assert c.close[-1] == 1.1


class FakeAlpha:
    def __init__(self, series, fail=False):
        self.series, self.fail, self.tokens_by_id = series, fail, {}

    def tokens(self, min_volume, min_liquidity, count):
        if self.fail:
            raise RuntimeError("Binance Alpha -> HTTP 451")
        self.tokens_by_id = {
            "ALPHA_7": {"id": "ALPHA_7", "symbol": "PUMP", "name": "Pump", "chain": "BSC",
                        "change_24h": 31.0, "volume": 9e5, "liquidity": 3e5, "mcap": 8e6,
                        "holders": 4100, "listed_spot": False}}
        return list(self.tokens_by_id.values())

    def klines(self, alpha_id, interval, need):
        return self.series[alpha_id]


def run(tmp_path, monkeypatch, data):
    pump = quiet_then_breakout()
    monkeypatch.setattr(MarketData, "klines",
                        lambda self, sym, interval, total, now_ms=None: flat_btc(len(pump)))
    data.series = {"ALPHA_7": pump}
    sent = []

    class Tg:
        enabled = True

        def send(self, text):
            sent.append(text)
            return True

    class Ctx:
        def get(self):
            return context.MarketContext()

    cfg = dict(cli.DEFAULTS, radar_interval="1h", state_file=str(tmp_path / "state.json"),
               signals_file=str(tmp_path / "signals.json"), alpha_file=str(tmp_path / "alpha.json"))
    n = cli.alpha_once(cfg, MarketData(), data, Tg(), Ctx())
    return n, sent, json.loads((tmp_path / "alpha.json").read_text()), cfg


def test_alpha_explosion_alert_and_file(tmp_path, monkeypatch):
    data = FakeAlpha({})
    n, sent, out, cfg = run(tmp_path, monkeypatch, data)
    assert n == 1
    first = sent[0].splitlines()[0]
    assert "EXPLOSION BINANCE ALPHA - PUMPUSDT (1h)" in first
    assert "Pump sur BSC" in sent[0] and "4100 detenteurs" in sent[0]
    assert "Niveaux et flux d'ordres :" in sent[0] and "Carnet d'ordres" not in sent[0]
    assert sent[0].rstrip().endswith("quelques heures.")
    row = out["rows"][0]
    assert out["available"] and out["scanned"] == 1 and row["signal"] and row["change_24h"] == 31.0
    assert row["info"][0] == "Pump sur BSC" and row["exchanges"] is None
    assert json.loads((tmp_path / "signals.json").read_text())[0]["kind"] == "alpha"
    assert json.loads((tmp_path / "state.json").read_text()).keys() == {"alpha:ALPHA_7"}
    assert cli.alpha_once(cfg, MarketData(), data, type("T", (), {"enabled": False})(),
                          type("C", (), {"get": lambda self: context.MarketContext()})()) == 0


def test_alpha_unreachable_is_reported_not_fatal(tmp_path, monkeypatch):
    n, sent, out, _ = run(tmp_path, monkeypatch, FakeAlpha({}, fail=True))
    assert n == 0 and sent == [] and out["available"] is False and out["rows"] == []
