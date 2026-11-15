import json

import smart_trader_pro.context as context
from smart_trader_pro import cli
from smart_trader_pro.data import MarketData
from smart_trader_pro.notify import format_signal
from smart_trader_pro.strategy import Params, evaluate, prepare, warmup
from conftest import HOUR, make_candles


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, ""

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


def kline_rows(c, upto=None):
    rows = []
    for i in range(len(c) if upto is None else upto):
        rows.append([c.ts[i], str(c.open[i]), str(c.high[i]), str(c.low[i]),
                     str(c.close[i]), str(c.volume[i]), c.ts[i] + HOUR - 1])
    return rows


def test_klines_paging_and_drops_open_candle(monkeypatch):
    c = make_candles(n=2300)
    rows = kline_rows(c)
    md = MarketData(base_urls=["https://x"])
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append(params)
        pool = [r for r in rows if params.get("endTime") is None or r[0] <= params["endTime"]]
        return FakeResponse(pool[-params["limit"]:])

    monkeypatch.setattr(md.session, "get", fake_get)
    now = c.ts[-1] + 10                      # la derniere bougie est encore ouverte
    out = md.klines("TESTUSDT", "1h", total=2000, now_ms=now)
    assert len(calls) == 3 and len(out) == 2000
    assert out.ts[-1] == c.ts[-2] and out.ts == sorted(set(out.ts))
    assert out.close[-1] == c.close[-2]


def test_top_symbols_filters(monkeypatch):
    md = MarketData(base_urls=["https://x"])
    tick = [{"symbol": "BTCUSDT", "quoteVolume": "9e9"}, {"symbol": "USDCUSDT", "quoteVolume": "8e9"},
            {"symbol": "ETHUSDT", "quoteVolume": "5e9"}, {"symbol": "ETHBTC", "quoteVolume": "7e9"},
            {"symbol": "BTCUPUSDT", "quoteVolume": "6e9"}, {"symbol": "TINYUSDT", "quoteVolume": "10"},
            {"symbol": "RLUSDUSDT", "quoteVolume": "4e9"},
            {"symbol": "UUSDT", "quoteVolume": "3e9", "lowPrice": "0.9995", "highPrice": "1.0004"},
            {"symbol": "GOOGLBUSDT", "quoteVolume": "2e9"}, {"symbol": "QQQBUSDT", "quoteVolume": "2e9"},
            {"symbol": "BNBUSDT", "quoteVolume": "1e9"}]
    monkeypatch.setattr(md.session, "get", lambda *a, **k: FakeResponse(tick))
    assert md.top_symbols(count=5) == ["BTCUSDT", "ETHUSDT", "BNBUSDT"]   # BNB reste : ce n'est pas une action


def test_context_sources_and_failures(monkeypatch):
    monkeypatch.setenv("CMC_API_KEY", "k")
    prov = context.ContextProvider()

    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/global"):
            return FakeResponse({"data": {"market_cap_change_percentage_24h_usd": -6.2,
                                          "market_cap_percentage": {"btc": 55.0}}})
        if url.endswith("/search/trending"):
            return FakeResponse({"coins": [{"item": {"symbol": "sol"}}]})
        if "global-metrics" in url:
            assert headers["X-CMC_PRO_API_KEY"] == "k"
            return FakeResponse({"data": {"btc_dominance": 56.5, "quote": {"USD": {}}}})
        return FakeResponse({}, status=500)          # indice Peur et Avidite en panne

    monkeypatch.setattr(prov.session, "get", fake_get)
    ctx = prov.get()
    assert ctx.sources == ["CoinGecko", "CoinMarketCap"] and len(ctx.errors) == 1
    assert ctx.btc_dominance == 56.5 and ctx.risk_off(5) and not ctx.risk_off(7)
    assert any("SOL" in l for l in ctx.lines("SOLUSDT"))
    assert prov.get() is ctx                         # cache


def test_scan_once_end_to_end(monkeypatch, tmp_path, capsys):
    c = make_candles()
    p = Params()
    f = prepare(c, p)
    idx = next(i for i in range(warmup(p), len(c)) if evaluate(f, i, p).is_signal)
    md = MarketData(base_urls=["https://x"])
    monkeypatch.setattr(md.session, "get",
                        lambda url, params=None, timeout=None:
                        FakeResponse(kline_rows(c, idx + 1)[-params["limit"]:]))
    sent = []

    class Tg:
        enabled = True

        def send(self, text):
            sent.append(text)
            return True

    class Ctx:
        def get(self):
            return context.MarketContext(market_change_24h=1.0)

    cfg = dict(cli.DEFAULTS, symbols=["TESTUSDT"], btc_filter=False, radar=False,
               state_file=str(tmp_path / "state.json"))
    monkeypatch.setattr("smart_trader_pro.data.time.time", lambda: (c.ts[idx] + HOUR + 5) / 1000)
    assert cli.scan_once(cfg, md, Tg(), Ctx()) == 1
    assert "SIGNAL D'ENTREE - TESTUSDT" in sent[0] and "Stop :" in sent[0]
    assert json.loads((tmp_path / "state.json").read_text())["TESTUSDT"] == c.ts[idx]
    assert cli.scan_once(cfg, md, Tg(), Ctx()) == 0 and len(sent) == 1   # pas de doublon

    class Crash(Ctx):
        def get(self):
            return context.MarketContext(market_change_24h=-9.0)

    (tmp_path / "state.json").unlink()
    assert cli.scan_once(cfg, md, Tg(), Crash()) == 0 and "suspendu" in capsys.readouterr().out


def test_message_format(candles):
    p = Params()
    f = prepare(candles, p)
    ev = next(e for e in (evaluate(f, i, p) for i in range(warmup(p), len(candles))) if e.is_signal)
    text = format_signal(ev, "1h", 1000, 1)
    assert "Score :" in text and "Objectif 2" in text and "Aucun ordre" in text
