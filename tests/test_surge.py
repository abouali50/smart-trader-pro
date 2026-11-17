import json

from smart_trader_pro import cli, surge
from smart_trader_pro.data import Candles, MarketData
from conftest import HOUR

T0 = 1_700_006_400_000
QUARTER = 900_000


def series(symbol="GEMUSDT", n=130, high=1.00):
    c = Candles(symbol, "15m")
    for i in range(n):
        c.append(T0 + i * QUARTER, 0.97, high if i == n - 40 else 0.985, 0.96, 0.975, 1000, 500)
    return c


def test_detect_needs_both_price_and_volume():
    old = {"GEMUSDT": (1.00, 1_440_000.0), "FLATUSDT": (2.0, 5_000_000.0), "THINUSDT": (1.0, 100_000.0),
           "PRICEONLYUSDT": (1.0, 2_000_000.0)}
    new = {"GEMUSDT": (1.03, 1_470_000.0),          # +3 % et 10 000 par minute contre 1 020 d'habitude
           "FLATUSDT": (2.01, 5_300_000.0),         # gros volume, prix immobile
           "THINUSDT": (1.10, 130_000.0),           # trop peu liquide
           "PRICEONLYUSDT": (1.04, 2_001_000.0),    # prix qui saute sans volume
           "NEWUSDT": (1.0, 9_000_000.0)}           # absente de l'ancienne photo
    found = surge.detect(old, new, 3, 2.0, 8, 500_000)
    assert [c["symbol"] for c in found] == ["GEMUSDT"]
    assert abs(found[0]["move"] - 3.0) < 1e-9 and 9.5 < found[0]["ratio"] < 10
    assert surge.detect(old, new, 0, 2.0, 8, 500_000) == []


def test_plan_requires_a_real_breakout_and_is_not_late():
    cand = {"symbol": "GEMUSDT", "price": 1.03, "move": 3.0, "ratio": 9.8, "minutes": 3, "volume_24h": 1.47e6}
    p = surge.plan(cand, series())
    assert p["broken"] == 1.00 and p["stop"] < 1.03 < p["tp1"] < p["tp2"] and p["risk_pct"] >= 1.5
    assert abs((p["tp2"] - 1.03) / (1.03 - p["stop"]) - 3.0) < 1e-9
    assert p["ts"] == T0 + 130 * QUARTER                                  # bougie en cours
    assert surge.plan(dict(cand, price=0.995), series()) is None           # sous le plus haut de 24 h
    assert surge.plan(dict(cand, price=1.20), series()) is None            # deja +23 % : trop tard
    assert surge.plan(cand, series(n=50)) is None                          # historique trop court
    text = surge.format_alert(p, lambda x: f"{x:.4f}")
    assert text.startswith("⚡ DEPART EN COURS - GEMUSDT") and "Prix maintenant : 1.0300 (+3.0 % en 3 min)" in text
    assert "Entree : 1.0300 (prix au moment de l'alerte)" in text and "trop tard" in text


class FakeMarket:
    def __init__(self):
        self.snaps, self.klines_calls = [], 0

    def snapshot(self, quote="USDT"):
        return self.snaps.pop(0)

    def klines(self, symbol, interval, total, now_ms=None):
        self.klines_calls += 1
        return series(symbol)


def test_watcher_alerts_once_and_writes_the_signal(tmp_path):
    cfg = dict(cli.DEFAULTS, signals_file=str(tmp_path / "signals.json"))
    md = FakeMarket()
    quiet = {"GEMUSDT": (1.00, 1_440_000.0), "MSTRBUSDT": (1.0, 1_440_000.0)}
    hot = {"GEMUSDT": (1.03, 1_470_000.0), "MSTRBUSDT": (1.05, 1_480_000.0)}      # la seconde est exclue
    md.snaps = [quiet, dict(quiet), hot, {"GEMUSDT": (1.06, 1_510_000.0), "MSTRBUSDT": (1.05, 1_480_000.0)}]
    w = surge.SurgeWatcher(cfg, md)
    sent = []
    tg = type("Tg", (), {"enabled": True, "send": lambda self, text: sent.append(text) or True})()
    (tmp_path / "signals.json").write_text(json.dumps([{"kind": "radar", "symbol": "OLDUSDT", "ts": 1}]))
    assert cli.surge_once(cfg, w, tg, now=1000.0) == 0                     # premiere photo : rien a comparer
    assert cli.surge_once(cfg, w, tg, now=1060.0) == 0                     # trop recent
    assert cli.surge_once(cfg, w, tg, now=1180.0) == 1 and len(sent) == 1
    assert sent[0].startswith("⚡ DEPART EN COURS - GEMUSDT") and md.klines_calls == 1
    saved = json.loads((tmp_path / "signals.json").read_text())
    assert [s["kind"] for s in saved] == ["surge", "radar"] and saved[0]["interval"] == "15m"
    assert saved[0]["stop"] < saved[0]["price"] < saved[0]["tp2"]
    assert cli.surge_once(cfg, w, tg, now=1240.0) == 0 and len(sent) == 1  # meme crypto : une seule alerte


def test_tracking_does_not_erase_alerts_added_meanwhile(monkeypatch, tmp_path):
    """La voie rapide ecrit pendant que le suivi lit les bougies : rien ne doit etre perdu."""
    cfg = dict(cli.DEFAULTS, signals_file=str(tmp_path / "signals.json"),
               tracking_file=str(tmp_path / "tracking.json"))
    sig = {"kind": "radar", "interval": "1h", "symbol": "AUSDT", "ts": T0, "price": 100.0, "score": 80,
           "stop": 95.0, "tp1": 107.5, "tp2": 115.0}
    (tmp_path / "signals.json").write_text(json.dumps([sig]))

    def klines(self, sym, interval, total, now_ms=None):
        cli.add_signals(cfg, [{"kind": "surge", "symbol": "LATEUSDT", "ts": T0 + 5}])   # ecriture concurrente
        c = Candles(sym, "1h")
        c.append(T0, 100, 101, 99, 100, 1, 1)
        c.append(T0 + HOUR, 100, 116, 99, 115, 1, 1)
        return c

    monkeypatch.setattr(MarketData, "klines", klines)
    assert cli.track_once(cfg, MarketData(), None, now_ms=T0 + 3 * HOUR) == 1
    saved = json.loads((tmp_path / "signals.json").read_text())
    assert [s["symbol"] for s in saved] == ["LATEUSDT", "AUSDT"] and saved[1]["outcome"]["status"] == "tp2"
    assert json.loads((tmp_path / "tracking.json").read_text())["groups"]["surge"]["open"] == 1


def test_explosion_alert_says_when_it_is_too_late(monkeypatch, tmp_path):
    from smart_trader_pro import context
    from test_breakout import flat_btc, quiet_then_breakout
    pump = quiet_then_breakout()
    data = {"PUMPUSDT": pump, "BTCUSDT": flat_btc(len(pump))}
    monkeypatch.setattr(MarketData, "klines", lambda self, sym, interval, total, now_ms=None: data[sym])
    monkeypatch.setattr(MarketData, "top_symbols",
                        lambda self, quote="USDT", count=20, min_quote_volume=0: ["PUMPUSDT"])
    sent = []
    tg = type("Tg", (), {"enabled": True, "send": lambda self, text: sent.append(text) or True})()
    ctx = type("Ctx", (), {"get": lambda self: context.MarketContext()})()

    def run(live, name):
        monkeypatch.setattr(MarketData, "last_price", lambda self, sym: live)
        cfg = dict(cli.DEFAULTS, symbols=[], radar_interval="1h", pressure=False, exchanges=False,
                   state_file=str(tmp_path / f"{name}.json"), signals_file=str(tmp_path / f"g{name}.json"),
                   radar_file=str(tmp_path / f"r{name}.json"))
        assert cli.radar_once(cfg, MarketData(), tg, ctx) == 1
        return sent[-1], json.loads((tmp_path / f"g{name}.json").read_text())[0]

    text, sig = run(None, "a")
    assert "Prix actuel" not in text and "TROP TARD" not in text
    assert "Prix actuel : " in run(sig["price"] * 1.004, "b")[0]
    assert "TROP TARD" in run(sig["tp1"] * 1.01, "c")[0]
    assert "ANNULE" in run(sig["stop"] * 0.99, "d")[0]
