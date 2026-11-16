import copy
import json

import pytest

import smart_trader_pro.context as context
from smart_trader_pro import cli, levels
from smart_trader_pro.data import Candles, MarketData
from conftest import HOUR, make_candles
from test_breakout import T0, flat_btc, quiet_then_breakout
from test_io import FakeResponse


def bars(rows):
    c = Candles("XUSDT", "1h")
    for k, (o, h, l, cl, v, tb) in enumerate(rows):
        c.append(T0 + k * HOUR, o, h, l, cl, v, tb)
    return c


def test_vwap_weights_by_volume():
    c = bars([(10, 10, 10, 10, 100, 50), (20, 20, 20, 20, 300, 150)])
    assert levels.vwap(c, 1, 0) == pytest.approx(17.5)
    assert levels.vwap(c, 1, 1) == pytest.approx(20.0)
    assert levels.vwap(bars([(1, 1, 1, 1, 0, 0)]), 0, 0) is None


def test_anchored_vwap_starts_at_the_lowest_low():
    c = bars([(12, 13, 11, 12, 100, 50), (11, 12, 8, 9, 100, 50),
              (9, 11, 9, 10, 100, 50), (10, 12, 10, 12, 300, 150)])
    assert levels.anchor_index(c, 3) == 1
    info = levels.analyze(c, 3)
    assert info["anchor_ts"] == c.ts[1]
    assert info["avwap"] == pytest.approx(levels.vwap(c, 3, 1))
    assert "il y a 2 bougies" in "\n".join(info["lines"])


def test_volume_profile_finds_poc_and_value_area():
    rows = [(100, 100.4, 99.6, 100, 1000, 500)] * 40 + [(110, 110.4, 109.6, 110, 50, 25)] * 5 \
        + [(90, 90.4, 89.6, 90, 50, 25)] * 5
    prof = levels.volume_profile(bars(rows), len(rows) - 1, window=50, bins=20)
    assert 99 < prof["poc"] < 101
    assert prof["val"] <= prof["poc"] <= prof["vah"]
    assert prof["val"] > 95 and prof["vah"] < 105          # 70 % du volume est autour de 100
    assert levels.volume_profile(bars([(5, 5, 5, 5, 10, 5)] * 3), 2) is None


def test_order_flow_delta_and_missing_data():
    c = bars([(10, 11, 9, 10, 100, 50)] * 23 + [(10, 11, 9, 11, 200, 150)])
    flow = levels.order_flow(c, 23)
    assert flow["bar_buy"] == 0.75 and flow["bar_delta"] == 0.5
    assert flow["cvd"] == pytest.approx((2 * (23 * 50 + 150) - (23 * 100 + 200)) / (23 * 100 + 200))
    no_taker = bars([(10, 11, 9, 10, 100, 0)] * 5)
    assert levels.order_flow(no_taker, 4) is None
    assert "indisponible" in levels.analyze(no_taker, 4)["lines"][-1]


def test_analyze_reads_position_and_book():
    c = quiet_then_breakout()
    info = levels.analyze(c, len(c) - 1, book=0.62)
    text = "\n".join(info["lines"])
    assert info["book"] == 0.62 and "62 % d'achats en attente" in text
    assert "prix au-dessus" in text and "au-dessus de la zone de valeur" in text
    assert "72 % d'achats au marche" in text
    assert info["val"] < info["poc"] < info["vah"] < c.close[-1]


def test_levels_have_no_lookahead():
    c = make_candles(n=700, seed=9)
    i = 500
    before = levels.analyze(c, i)
    altered = copy.deepcopy(c)
    for k in range(i + 1, len(altered)):
        for name in ("open", "high", "low", "close", "volume", "taker_buy"):
            getattr(altered, name)[k] *= 5
    assert levels.analyze(altered, i) == before


def test_book_imbalance(monkeypatch):
    import conftest
    monkeypatch.setattr(MarketData, "book_imbalance", conftest.REAL_BOOK_IMBALANCE)
    md = MarketData(base_urls=["https://x"])
    depth = {"bids": [["99.9", "10"], ["99.5", "10"], ["90", "500"]],
             "asks": [["100.1", "5"], ["100.5", "5"], ["120", "500"]]}
    monkeypatch.setattr(md.session, "get", lambda url, params=None, timeout=None: FakeResponse(depth))
    b = 99.9 * 10 + 99.5 * 10
    a = 100.1 * 5 + 100.5 * 5
    assert md.book_imbalance("XUSDT") == pytest.approx(b / (a + b))    # ordres lointains ignores
    monkeypatch.setattr(md.session, "get", lambda url, params=None, timeout=None: FakeResponse({}, 500))
    monkeypatch.setattr("smart_trader_pro.data.time.sleep", lambda s: None)
    assert md.book_imbalance("XUSDT") is None
    monkeypatch.setattr(md.session, "get", lambda url, params=None, timeout=None: FakeResponse([[1, 2]]))
    assert md.book_imbalance("XUSDT") is None            # format inattendu : tolere


def test_scan_adds_levels_to_rows_and_alerts(monkeypatch, tmp_path):
    pump = quiet_then_breakout()
    series = {"PUMPUSDT": pump, "BTCUSDT": flat_btc(len(pump))}
    monkeypatch.setattr(MarketData, "klines", lambda self, sym, interval, total, now_ms=None: series[sym])
    monkeypatch.setattr(MarketData, "top_symbols",
                        lambda self, quote="USDT", count=20, min_quote_volume=0: ["PUMPUSDT"])
    monkeypatch.setattr(MarketData, "book_imbalance", lambda self, sym, pct=1.0: 0.7)
    sent = []

    class Tg:
        enabled = True

        def send(self, text):
            sent.append(text)
            return True

    class Ctx:
        def get(self):
            return context.MarketContext()

    cfg = dict(cli.DEFAULTS, symbols=[], btc_filter=False, radar_interval="1h",
               state_file=str(tmp_path / "state.json"),
               status_file=str(tmp_path / "status.json"),
               signals_file=str(tmp_path / "signals.json"),
               radar_file=str(tmp_path / "radar.json"))
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 1
    assert "Niveaux et flux d'ordres :" in sent[0] and "VWAP ancre au dernier creux" in sent[0]
    assert "Profil de volume : POC" in sent[0] and "70 % d'achats en attente" in sent[0]
    row = json.loads((tmp_path / "radar.json").read_text())["rows"][0]
    assert row["levels"]["book"] == 0.7 and len(row["levels"]["lines"]) == 6
