import json

from smart_trader_pro import cli, tracker
from smart_trader_pro.data import Candles, MarketData
from conftest import HOUR

T0 = 1_700_006_400_000


def path(bars, symbol="AUSDT"):
    """bars : liste de (open, high, low, close), la premiere etant la bougie du signal."""
    c = Candles(symbol, "1h")
    for i, (o, h, l, cl) in enumerate(bars):
        c.append(T0 + i * HOUR, o, h, l, cl, 1000, 500)
    return c


def sig(**kw):
    base = {"kind": "radar", "interval": "1h", "symbol": "AUSDT", "ts": T0, "price": 100.0,
            "score": 80, "stop": 95.0, "tp1": 107.5, "tp2": 115.0}
    base.update(kw)
    return base


def test_settle_replays_the_announced_plan():
    flat = (100, 101, 99, 100)
    assert tracker.settle(sig(), path([flat])) is None                    # aucune bougie apres le signal
    win = tracker.settle(sig(), path([flat, flat, (100, 108, 99, 107), (107, 116, 106, 115)]))
    assert win["status"] == "tp2" and win["tp1"] and win["entry"] == 100 and win["bars"] == 3
    assert 2.9 < win["r"] < 3.0 and 14.7 < win["pct"] < 15.0              # +3R moins les frais
    loss = tracker.settle(sig(), path([flat, (100, 101, 94, 96)]))
    assert loss["status"] == "stop" and -1.1 < loss["r"] < -1.0
    both = tracker.settle(sig(), path([flat, (100, 116, 94, 100)]))       # stop et objectif : stop
    assert both["status"] == "stop"
    live = tracker.settle(sig(), path([flat, (100, 103, 99, 102)]))
    assert live["status"] == "open" and abs(live["pct"] - 2.0) < 1e-9 and abs(live["max_pct"] - 3.0) < 1e-9
    old = tracker.settle(sig(), path([flat] + [(100, 102, 99, 101)] * 5), max_bars=5)
    assert old["status"] == "expired" and old["exit"] == 101 and 0 < old["r"] < 0.2
    assert tracker.settle(sig(), path([flat, (90, 91, 89, 90)]))["status"] == "invalid"   # ouverture sous le stop
    gap = path([flat])
    gap.append(T0 + 5 * HOUR, 100, 101, 99, 100, 1000, 500)
    assert tracker.settle(sig(), gap)["status"] == "unknown"              # bougies manquantes
    # Une bougie ajoutee apres la sortie ne change rien au resultat.
    more = tracker.settle(sig(), path([flat, (100, 101, 94, 96), (96, 130, 96, 129)]))
    assert more == loss


def test_update_checks_closes_and_summarises():
    flat = (100, 101, 99, 100)
    data = {"AUSDT": path([flat, (100, 116, 99, 115)]),
            "BUSDT": path([flat, (100, 101, 94, 95)], "BUSDT"),
            "CUSDT": path([flat, (100, 102, 99, 101)], "CUSDT")}
    calls = []

    def fetch(s, interval, total):
        calls.append(s["symbol"])
        if s["symbol"] == "ZUSDT":
            raise RuntimeError("paire retiree")
        return data[s["symbol"]]

    signals = [sig(), sig(symbol="BUSDT"), sig(symbol="CUSDT", kind="repli"),
               sig(symbol="ZUSDT"), sig(symbol="NEWUSDT", ts=T0 + 3 * HOUR), {"symbol": "XUSDT"}]
    now = T0 + 3 * HOUR
    checked, closed = tracker.update(signals, fetch, now, lambda s: s["interval"])
    assert (checked, closed) == (4, 2) and "NEWUSDT" not in calls         # trop recente
    assert [s["outcome"]["status"] for s in signals[:3]] == ["tp2", "stop", "open"]
    assert signals[5]["outcome"]["status"] == "unknown"
    assert signals[2]["next_check"] == now + tracker.RECHECK_BARS * HOUR
    calls.clear()
    assert tracker.update(signals, fetch, now + HOUR, lambda s: s["interval"]) == (1, 0)
    assert calls == ["ZUSDT"]                # ni les clotures, ni l'alerte ouverte deja relue
    for k in range(tracker.MAX_FAILURES):
        tracker.update(signals, fetch, now + (k + 2) * HOUR, lambda s: s["interval"])
    assert signals[3]["outcome"]["status"] == "unknown"
    g = tracker.summary(signals)
    assert g["radar"]["closed"] == 2 and g["radar"]["wins"] == 1 and g["radar"]["win_rate"] == 50
    assert g["radar"]["tp2"] == 1 and g["radar"]["stop"] == 1 and 0.9 < g["radar"]["avg_r"] < 1.0
    assert g["repli"]["closed"] == 0 and g["repli"]["open"] == 1 and g["repli"]["avg_r"] is None


def test_track_once_writes_results_and_tally(monkeypatch, tmp_path):
    flat = (100, 101, 99, 100)
    series = {"AUSDT": path([flat, (100, 116, 99, 115)])}
    monkeypatch.setattr(MarketData, "klines", lambda self, sym, interval, total, now_ms=None: series[sym])
    cfg = dict(cli.DEFAULTS, signals_file=str(tmp_path / "signals.json"),
               tracking_file=str(tmp_path / "tracking.json"))
    old = sig()
    del old["interval"]                                                    # alerte d'une ancienne version
    (tmp_path / "signals.json").write_text(json.dumps([dict(old, interval="1h"),
                                                       sig(kind="alpha", symbol="TOKUSDT")]))
    assert cli.track_once(cfg, MarketData(), None, now_ms=T0 + 3 * HOUR) == 1
    saved = json.loads((tmp_path / "signals.json").read_text())
    assert saved[0]["outcome"]["status"] == "tp2" and "outcome" not in saved[1]   # Alpha inactif : a revoir
    tally = json.loads((tmp_path / "tracking.json").read_text())
    assert tally["groups"]["radar"]["closed"] == 1 and tally["groups"]["alpha"]["open"] == 1
    assert tally["max_bars"] == tracker.MAX_BARS
