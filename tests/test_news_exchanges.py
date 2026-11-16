import json
import time
from email.utils import formatdate

import smart_trader_pro.context as context
from smart_trader_pro import cli, exchanges, news
from smart_trader_pro.data import MarketData
from test_breakout import T0, flat_btc, quiet_then_breakout
from test_io import FakeResponse
from conftest import HOUR


def rss(items):
    body = "".join(f"<item><title>{t}</title><link>{l}</link><pubDate>{formatdate(ts)}</pubDate></item>"
                   for t, l, ts in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'


class Text(FakeResponse):
    def __init__(self, text, status=200):
        super().__init__(None, status)
        self.text = text


def test_match_coins_avoids_common_words():
    names = {"SOL": "Solana", "LINK": "Chainlink", "NEAR": "NEAR Protocol", "BTC": "Bitcoin",
             "ONE": "Harmony", "TRUMP": "Official Trump"}
    assert news.match_coins("Solana jumps 12% as SOL ETF flows surge", names) == ["SOL"]
    assert news.match_coins("Chainlink partners with a major bank", names) == ["LINK"]
    assert news.match_coins("Read more at this LINK, the end is NEAR for ONE fund", names) == []
    assert news.match_coins("Traders pile into $LINK and bitcoin", names) == ["LINK", "BTC"]
    assert news.match_coins("Trump signs crypto bill", names) == []
    assert news.match_coins("SOLID results, ABSOLUTE record", names) == []


def test_parse_rss_rejects_entities_and_bad_items():
    now = time.time()
    good = rss([("Bitcoin rises", "https://x.test/a", now), ("No link", "ftp://x", now)])
    items = news.parse_rss(good, "CoinDesk")
    assert [i.title for i in items] == ["Bitcoin rises"] and items[0].source == "CoinDesk"
    assert news.parse_rss('<!DOCTYPE x [<!ENTITY a "b">]><rss/>', "X") == []
    assert news.parse_rss("pas du xml", "X") == []


def test_news_provider_filters_dedupes_and_survives_failures(monkeypatch):
    now = time.time()
    prov = news.NewsProvider(feeds={"A": "https://a.test/rss", "B": "https://b.test/rss"})

    def fake_get(url, timeout=None, params=None):
        if "coingecko" in url:
            return FakeResponse([{"symbol": "sol", "name": "Solana"}, {"symbol": "btc", "name": "Bitcoin"}])
        if "a.test" in url:
            return Text(rss([("Solana breaks out", "https://a.test/1", now - 600),
                             ("Old Bitcoin story", "https://a.test/2", now - 3 * 86400),
                             ("Markets calm", "https://a.test/3", now - 60)]))
        return Text("", status=503)

    monkeypatch.setattr(prov.session, "get", fake_get)
    items = prov.latest(["SOLUSDT", "BTCUSDT"])
    assert [i.title for i in items] == ["Markets calm", "Solana breaks out"]
    assert items[1].coins == ["SOL"] and len(prov.errors) == 1
    assert [i.title for i in news.for_coin(items, "SOLUSDT")] == ["Solana breaks out"]
    assert prov.latest(["SOLUSDT"]) is items                      # cache


def test_exchange_ratio_uses_only_closed_hours():
    now = 1_800_000_000
    start = now - (now % 3600)
    candles = [(start - k * 3600, 100.0) for k in range(1, 25)]
    candles[0] = (start - 3600, 450.0)                 # derniere heure close
    candles.append((start, 9999.0))                    # heure en cours : ignoree
    assert exchanges._ratio(candles, now) == 4.5
    assert exchanges._ratio(candles[:5], now) is None  # pas assez d'historique


def test_confirmations_parse_each_exchange_and_tolerate_failures(monkeypatch):
    now = 1_800_000_000
    start = now - (now % 3600)
    ex = exchanges.Exchanges()

    def fake_get(url, params=None, timeout=None):
        hours = [start - k * 3600 for k in range(0, 30)]            # du plus recent au plus ancien
        vol = lambda k: 300.0 if k == 1 else 100.0
        if "coinbase" in url:
            assert url.endswith("/products/SOL-USD/candles")
            return FakeResponse([[t, 1, 2, 1, 1.5, vol(k)] for k, t in enumerate(hours)])
        if "okx" in url:
            assert params["instId"] == "SOL-USDT"
            return FakeResponse({"data": [[str(t * 1000), "1", "2", "1", "1.5", str(100.0), "0", "0", "1"]
                                          for t in hours]})
        return FakeResponse({"label": "INVALID_CURRENCY"}, status=400)   # Gate : non cotee

    monkeypatch.setattr(ex.session, "get", fake_get)
    conf = {c["name"]: c for c in ex.confirmations("SOLUSDT", now=now)}
    assert conf["Coinbase"]["ratio"] == 3.0 and conf["Coinbase"]["confirmed"]
    assert conf["OKX"]["ratio"] == 1.0 and not conf["OKX"]["confirmed"]
    assert conf["Gate.io"]["ratio"] is None
    lines = exchanges.summary_lines(list(conf.values()))
    assert lines[0] == "Autres plateformes : volume confirme sur 1 sur 2" and len(lines) == 3
    assert "aucune donnee" in exchanges.summary_lines([{"name": "X", "ratio": None, "confirmed": False}])[0]


def test_scan_attaches_news_and_exchange_confirmation(monkeypatch, tmp_path):
    pump = quiet_then_breakout()
    series = {"PUMPUSDT": pump, "BTCUSDT": flat_btc(len(pump)), "MSTRBUSDT": pump}
    monkeypatch.setattr(MarketData, "klines", lambda self, sym, interval, total, now_ms=None: series[sym])
    monkeypatch.setattr(MarketData, "top_symbols",
                        lambda self, quote="USDT", count=20, min_quote_volume=0: ["MSTRBUSDT", "PUMPUSDT"])
    sent = []

    class Tg:
        enabled = True

        def send(self, text):
            sent.append(text)
            return True

    class Ctx:
        def get(self):
            return context.MarketContext()

    class News:
        errors = []

        def latest(self, symbols):
            assert "PUMPUSDT" in symbols and "MSTRBUSDT" not in symbols      # exclue
            return [news.NewsItem("Pump token lists on a major exchange", "https://n.test/1",
                                  "CoinDesk", int(time.time() * 1000), ["PUMP"]),
                    news.NewsItem("Unrelated", "https://n.test/2", "Decrypt", int(time.time() * 1000), [])]

    class Ex:
        def confirmations(self, sym, now=None):
            return [{"name": "Coinbase", "ratio": 4.2, "confirmed": True},
                    {"name": "OKX", "ratio": None, "confirmed": False}]

    cfg = dict(cli.DEFAULTS, symbols=[], btc_filter=False, radar_interval="1h",
               state_file=str(tmp_path / "state.json"),
               status_file=str(tmp_path / "status.json"),
               signals_file=str(tmp_path / "signals.json"),
               radar_file=str(tmp_path / "radar.json"))
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx(), News(), Ex()) == 1
    assert "Autres plateformes : volume confirme sur 1 sur 1" in sent[0]
    assert "CoinDesk : Pump token lists on a major exchange" in sent[0]
    radar = json.loads((tmp_path / "radar.json").read_text())
    assert radar["scanned"] == 1
    row = radar["rows"][0]
    assert row["exchanges"][0]["ratio"] == 4.2 and row["news"][0]["source"] == "CoinDesk"
