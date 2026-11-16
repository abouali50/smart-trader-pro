import copy
import json
import random

import smart_trader_pro.context as context
from smart_trader_pro import backtest, cli, compare, pressure
from smart_trader_pro.data import Candles, MarketData
from conftest import HOUR, make_candles
from test_breakout import T0, flat_btc


def accumulation(n_wild=200, n_calm=96, seed=3, buy=0.60, symbol="COILUSDT", breakout=False):
    """Zone agitee, puis accumulation calme sous une resistance, acheteurs dominants."""
    rng = random.Random(seed)
    c = Candles(symbol, "1h")
    price = 10.0
    for i in range(n_wild):                                   # marche agite, equilibre
        o = price
        cl = 10.0 + rng.uniform(-0.18, 0.18)
        v = 1000 * rng.uniform(0.8, 1.2)
        c.append(T0 + i * HOUR, o, max(o, cl) + 0.03, min(o, cl) - 0.03, cl, v, v * 0.5)
        price = cl
    for k in range(n_calm):                                   # accumulation : calme et haussiere
        o = price
        cl = 10.0 + 0.2 * (k + 1) / n_calm + rng.uniform(-0.004, 0.004)
        hi = max(o, cl) + 0.004
        if k == 30:
            hi = 10.30                                        # meche : la resistance
        v = (1500 if k >= n_calm - 12 else 1000) * rng.uniform(0.95, 1.05)
        c.append(T0 + (n_wild + k) * HOUR, o, hi, min(o, cl) - 0.004, cl, v, v * buy)
        price = cl
    if breakout:
        c.append(T0 + (n_wild + n_calm) * HOUR, price, 10.6, price, 10.55, 5000, 3500)
    return c


def test_accumulation_under_resistance_is_flagged():
    c = accumulation()
    p = pressure.Params()
    ev = pressure.evaluate(pressure.prepare(c, p, btc=flat_btc(len(c))), len(c) - 1, p)
    assert ev.is_signal and ev.score >= 80
    assert sum(t for _, t in ev.families.values()) == 100
    assert sum(g for g, _ in ev.families.values()) == ev.score
    assert set(ev.families) == {"compression", "accumulation", "volume", "structure", "force"}
    assert ev.trigger == 10.30 and ev.price < ev.trigger          # pas encore cassee
    assert ev.stop < ev.price < ev.tp1 < ev.tp2
    text = " | ".join(ev.reasons)
    assert "delta cumule +20 %" in text and "prix colle sous sa resistance" in text
    assert "volume qui se reveille" in text and "creux de plus en plus hauts" in text


def test_no_signal_without_buyers_or_after_the_move():
    p = pressure.Params()
    sellers = accumulation(buy=0.40)
    ev = pressure.evaluate(pressure.prepare(sellers, p), len(sellers) - 1, p)
    assert not ev.is_signal and ev.families["accumulation"][0] <= 10
    gone = accumulation(breakout=True)
    ev = pressure.evaluate(pressure.prepare(gone, p), len(gone) - 1, p)
    assert not ev.is_signal and ev.price > ev.trigger             # deja partie : trop tard
    wild = make_candles(n=600, seed=21)
    f = pressure.prepare(wild, p)
    flagged = sum(1 for i in range(pressure.warmup(p), len(wild))
                  if (e := pressure.evaluate(f, i, p)) and e.is_signal)
    assert flagged == 0                                           # pas de donnee d'acheteurs


def test_pressure_has_no_lookahead():
    c = accumulation()
    p = pressure.Params()
    i = len(c) - 20
    before = pressure.evaluate(pressure.prepare(c, p), i, p)
    altered = copy.deepcopy(c)
    for k in range(i + 1, len(altered)):
        for name in ("open", "high", "low", "close", "volume", "taker_buy"):
            getattr(altered, name)[k] *= 3
    after = pressure.evaluate(pressure.prepare(altered, p), i, p)
    assert (before.score, before.reasons, before.stop, before.trigger) == \
           (after.score, after.reasons, after.stop, after.trigger)


def test_backtest_and_compare_accept_the_pressure_engine():
    c = accumulation(breakout=True)
    rep = backtest.run(c, pressure.Params(), tp_r=3.0, engine=pressure)
    assert rep.count >= 1 and rep.trades[0].r > 0                 # l'accumulation a casse vers le haut
    data = [make_candles(n=2600, seed=s, symbol=f"P{s}USDT") for s in (1, 2)]
    results = compare.run(data, None, pressure.Params(), engine=pressure)
    assert len(results) == 24 and "calme" in results[0].label
    assert "pressure_strategy" in pressure.config_snippet(pressure.Params())


def test_scan_sends_pre_explosion_alerts_with_a_cap(monkeypatch, tmp_path):
    coils = {f"C{k}USDT": accumulation(seed=k, symbol=f"C{k}USDT") for k in range(1, 6)}
    n = len(next(iter(coils.values())))
    series = dict(coils, BTCUSDT=flat_btc(n))
    monkeypatch.setattr(MarketData, "klines", lambda self, sym, interval, total, now_ms=None: series[sym])
    monkeypatch.setattr(MarketData, "top_symbols",
                        lambda self, quote="USDT", count=20, min_quote_volume=0: sorted(coils))
    sent = []

    class Tg:
        enabled = True

        def send(self, text):
            sent.append(text)
            return True

    class Ctx:
        def get(self):
            return context.MarketContext()

    cfg = dict(cli.DEFAULTS, radar_interval="1h", pressure_max_alerts=2,
               state_file=str(tmp_path / "state.json"), signals_file=str(tmp_path / "signals.json"),
               radar_file=str(tmp_path / "radar.json"))
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 2            # plafonne a 2 par passage
    first = sent[0]
    assert "PRE-EXPLOSION SIGNAL BINANCE - C" in first.splitlines()[0]
    assert "Pas encore partie. Niveau a casser : 10.3000" in first
    assert "attendre une cloture au-dessus" in first and "Ce n'est pas une prediction." in first
    out = json.loads((tmp_path / "radar.json").read_text())
    rows = out["pressure"]["rows"]
    assert len(rows) == 5 and all(r["signal"] and r["trigger"] == 10.30 for r in rows)
    assert out["pressure"]["min_score"] == 75 and out["rows"][0]["signal"] is False
    kinds = {s["kind"] for s in json.loads((tmp_path / "signals.json").read_text())}
    assert kinds == {"pre-radar"}
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 2 and len(sent) == 4   # les suivantes
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 1 and len(sent) == 5
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 0                      # delai respecte
    off = dict(cfg, pressure=False, radar_file=str(tmp_path / "r2.json"), state_file=str(tmp_path / "s2.json"))
    assert cli.radar_once(off, MarketData(), Tg(), Ctx()) == 0
    assert json.loads((tmp_path / "r2.json").read_text())["pressure"]["rows"] == []
