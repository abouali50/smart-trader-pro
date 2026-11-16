import copy
import json
import random

import smart_trader_pro.context as context
from smart_trader_pro import backtest, breakout, cli, compare
from smart_trader_pro.data import Candles, MarketData
from conftest import HOUR, make_candles
from test_io import FakeResponse

T0 = 1_700_006_400_000


def quiet_then_breakout(n=260, seed=5, spike=True, symbol="PUMPUSDT"):
    """Marche plat a faible volume, puis une bougie qui casse par le haut avec gros volume."""
    rng = random.Random(seed)
    c = Candles(symbol, "1h")
    price = 10.0
    for i in range(n):
        o = price
        cl = 10.0 + rng.uniform(-0.03, 0.03)
        h, l = max(o, cl) + 0.01, min(o, cl) - 0.01
        v = 1000 * rng.uniform(0.8, 1.2)
        c.append(T0 + i * HOUR, o, h, l, cl, v, v * 0.5)
        price = cl
    if spike:
        o = price
        cl = 10.45
        c.append(T0 + n * HOUR, o, cl + 0.01, o - 0.01, cl, 9000.0, 9000.0 * 0.72)
    return c


def flat_btc(n):
    b = Candles("BTCUSDT", "1h")
    for i in range(n):
        b.append(T0 + i * HOUR, 100, 100.1, 99.9, 100, 500, 250)
    return b


def test_breakout_is_detected_with_levels():
    c = quiet_then_breakout()
    p = breakout.Params()
    f = breakout.prepare(c, p, btc=flat_btc(len(c)))
    ev = breakout.evaluate(f, len(c) - 1, p)
    assert ev.is_signal and ev.score >= 90
    assert sum(t for _, t in ev.families.values()) == 100
    assert sum(g for g, _ in ev.families.values()) == ev.score
    assert set(ev.families) == {"volume", "acheteurs", "cassure", "momentum", "force", "risque"}
    assert ev.stop < ev.price < ev.tp1 < ev.tp2
    assert any("achats au marche : 72 %" in r for r in ev.reasons)
    assert any("ecart avec le BTC" in r for r in ev.reasons)


def test_no_signal_without_breakout_or_on_weak_volume():
    p = breakout.Params()
    quiet = quiet_then_breakout(spike=False)
    f = breakout.prepare(quiet, p)
    evs = [breakout.evaluate(f, i, p) for i in range(breakout.warmup(p), len(quiet))]
    assert evs and not any(e.is_signal for e in evs if e)
    weak = quiet_then_breakout()
    weak.volume[-1], weak.taker_buy[-1] = 1000.0, 450.0     # cassure sans volume
    ev = breakout.evaluate(breakout.prepare(weak, p), len(weak) - 1, p)
    assert not ev.is_signal and ev.score < p.min_score


def test_too_late_loses_risk_points():
    c = quiet_then_breakout()
    c.close[-25] = 8.0                                       # deja +30 % sur 24 bougies
    p = breakout.Params()
    ev = breakout.evaluate(breakout.prepare(c, p), len(c) - 1, p)
    assert ev.families["risque"] == (0, 5)


def test_radar_has_no_lookahead():
    c = make_candles(n=900, seed=11)
    p = breakout.Params()
    i = 600
    before = breakout.evaluate(breakout.prepare(c, p), i, p)
    altered = copy.deepcopy(c)
    for k in range(i + 1, len(altered)):
        for name in ("open", "high", "low", "close", "volume", "taker_buy"):
            getattr(altered, name)[k] *= 4
    after = breakout.evaluate(breakout.prepare(altered, p), i, p)
    assert before is not None
    assert (before.score, before.reasons, before.stop) == (after.score, after.reasons, after.stop)


def test_prior_max_excludes_current_bar():
    assert breakout._prior_max([1, 5, 2, 3, 9, 4], 3) == [None, None, None, 5, 5, 9]


def test_backtest_and_compare_with_radar_engine():
    data = [make_candles(n=3000, seed=s, symbol=f"R{s}USDT") for s in (4, 5)]
    p = breakout.Params(min_score=50)
    fast = backtest.run(data[0], p, tp_r=3.0, engine=breakout)
    assert fast.count > 0
    f = breakout.prepare(data[0], p)
    full_signals = sum(1 for i in range(breakout.warmup(p), len(data[0]))
                       if (e := breakout.evaluate(f, i, p)) and e.is_signal)
    fast_signals = sum(1 for i in range(breakout.warmup(p), len(data[0]))
                       if (e := breakout.evaluate_fast(f, i, p)) and e.is_signal)
    assert full_signals == fast_signals >= fast.count        # le raccourci ne perd aucun signal
    results = compare.run(data, None, breakout.Params(), engine=breakout)
    assert len(results) == 72 and all(v.engine is breakout for v in results)
    assert "vol x" in results[0].label and "radar_strategy" in breakout.config_snippet(p)


def test_scan_runs_radar_and_reports_it(monkeypatch, tmp_path):
    pump = quiet_then_breakout()
    calm = quiet_then_breakout(spike=False, symbol="CALMUSDT")
    calm.append(T0 + 260 * HOUR, 10, 10.02, 9.98, 10.0, 1000, 500)
    series = {"PUMPUSDT": pump, "CALMUSDT": calm, "BTCUSDT": flat_btc(len(pump))}

    def klines(self, sym, interval, total, now_ms=None):
        return series[sym]

    monkeypatch.setattr(MarketData, "klines", klines)
    monkeypatch.setattr(MarketData, "top_symbols",
                        lambda self, quote="USDT", count=20, min_quote_volume=0: ["PUMPUSDT", "CALMUSDT"])
    sent = []

    class Tg:
        enabled = True

        def send(self, text):
            sent.append(text)
            return True

    class Ctx:
        def get(self):
            return context.MarketContext(market_change_24h=-6.0)   # marche en chute

    cfg = dict(cli.DEFAULTS, symbols=[], btc_filter=False, radar_interval="1h",
               state_file=str(tmp_path / "state.json"),
               status_file=str(tmp_path / "status.json"),
               signals_file=str(tmp_path / "signals.json"),
               radar_file=str(tmp_path / "radar.json"))
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 1     # le signal n'est pas bloque
    assert "EXPLOSION BINANCE - PUMPUSDT (1h)" in sent[0].splitlines()[0]
    radar = json.loads((tmp_path / "radar.json").read_text())
    assert radar["scanned"] == 2 and radar["interval"] == "1h" and radar["min_score"] == 70 and radar["rows"][0]["symbol"] == "PUMPUSDT"
    assert radar["rows"][0]["signal"] is True and radar["rows"][0]["change_24h"] > 3
    assert radar["rows"][1]["signal"] is False
    history = json.loads((tmp_path / "signals.json").read_text())
    assert history[0]["kind"] == "radar"
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 0 and len(sent) == 1  # delai respecte
    assert cli.scan_once(cfg, MarketData(), Tg(), Ctx()) == 0                      # replis : rien
    assert "radar" not in json.loads((tmp_path / "status.json").read_text())


def trending_btc(n, step):
    b = Candles("BTCUSDT", "1h")
    for i in range(n):
        px = 100 + step * i
        b.append(T0 + i * HOUR, px, px + 0.1, px - 0.1, px, 500, 250)
    return b


def test_market_filter_follows_bitcoin_trend():
    p = breakout.Params(market_bars=40)
    assert breakout.market_filter(flat_btc(200), breakout.Params()) is None        # coupe par defaut
    assert breakout.market_now(trending_btc(200, 0.5), p) == "up"
    assert breakout.market_now(trending_btc(200, -0.3), p) == "down"
    assert breakout.market_now(trending_btc(30, 0.5), p) is None                   # historique trop court
    down = trending_btc(200, -0.3)
    states = breakout.market_filter(down, p)
    assert states[down.ts[10]] == 0 and states[down.ts[-1]] == -1
    # Pas de regard vers le futur : l'etat a la bougie i ne change pas si on coupe la suite.
    cut = Candles("BTCUSDT", "1h")
    for i in range(120):
        cut.append(down.ts[i], down.open[i], down.high[i], down.low[i], down.close[i], 500, 250)
    assert breakout.market_states(cut, p) == breakout.market_states(down, p)[:120]
    assert breakout.market_need(p) > 3 * 40 and "BTC+" in breakout.label(p, 3.0)
    # Le backtest ne prend aucun trade quand le marche est baissier.
    pump = quiet_then_breakout()
    free = backtest.run(pump, breakout.Params(), engine=breakout)
    held = backtest.run(pump, p, engine=breakout,
                        market_trend={t: -1 for t in pump.ts})
    assert held.count == 0 <= free.count


def test_explosion_alert_is_held_when_bitcoin_falls(monkeypatch, tmp_path):
    pump = quiet_then_breakout()
    series = {"PUMPUSDT": pump, "BTCUSDT": trending_btc(len(pump), -0.2)}
    monkeypatch.setattr(MarketData, "klines", lambda self, sym, interval, total, now_ms=None: series[sym])
    monkeypatch.setattr(MarketData, "top_symbols",
                        lambda self, quote="USDT", count=20, min_quote_volume=0: ["PUMPUSDT"])
    sent = []

    class Tg:
        enabled = True

        def send(self, text):
            sent.append(text)
            return True

    class Ctx:
        def get(self):
            return context.MarketContext()

    cfg = dict(cli.DEFAULTS, symbols=[], radar_interval="1h", pressure=False,
               radar_strategy={"market_bars": 40},
               state_file=str(tmp_path / "state.json"),
               signals_file=str(tmp_path / "signals.json"),
               radar_file=str(tmp_path / "radar.json"))
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 0 and sent == []      # alerte retenue
    radar = json.loads((tmp_path / "radar.json").read_text())
    assert radar["market"] == "down"
    assert radar["rows"][0]["signal"] is False and radar["rows"][0]["suspended"] is True
    series["BTCUSDT"] = trending_btc(len(pump), 0.2)                               # le marche repart
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 1 and len(sent) == 1
    assert json.loads((tmp_path / "radar.json").read_text())["market"] == "up"
    off = dict(cfg, radar_strategy={"market_bars": 0}, state_file=str(tmp_path / "s2.json"))
    series["BTCUSDT"] = trending_btc(len(pump), -0.2)
    assert cli.radar_once(off, MarketData(), Tg(), Ctx()) == 1                     # filtre coupe
    assert json.loads((tmp_path / "radar.json").read_text())["market"] is None


def test_klines_reads_taker_buy_volume(monkeypatch):
    md = MarketData(base_urls=["https://x"])
    rows = [[T0 + i * HOUR, "1", "2", "0.5", "1.5", "100", T0 + (i + 1) * HOUR - 1,
             "150", 12, "64", "96", "0"] for i in range(5)]
    monkeypatch.setattr(md.session, "get", lambda url, params=None, timeout=None: FakeResponse(rows))
    c = md.klines("AUSDT", "1h", total=3, now_ms=T0 + 100 * HOUR)
    assert c.taker_buy == [64.0, 64.0, 64.0] and len(c.volume) == 3


def test_radar_defaults_are_tuned_for_15_minute_candles():
    p = cli.radar_params(dict(cli.DEFAULTS))
    assert cli.DEFAULTS["radar_interval"] == "15m"
    assert (p.breakout_lookback, p.rs_bars, p.late_bars) == (96, 96, 96)      # 96 x 15 min = 24 h
    custom = cli.radar_params(dict(cli.DEFAULTS, radar_strategy={"min_score": 80, "rs_bars": 48}))
    assert (custom.min_score, custom.rs_bars, custom.breakout_lookback) == (80, 48, 96)


def test_radar_skips_suspended_pairs_and_covers_all_by_default(monkeypatch, tmp_path):
    pump = quiet_then_breakout()
    stale = quiet_then_breakout(symbol="DEADUSDT")
    stale.ts = [t - 30 * 24 * HOUR for t in stale.ts]          # derniere bougie vieille d'un mois
    series = {"PUMPUSDT": pump, "DEADUSDT": stale, "BTCUSDT": flat_btc(len(pump))}
    asked = {}

    def top(self, quote="USDT", count=20, min_quote_volume=0):
        asked.update(count=count, min_volume=min_quote_volume)
        return ["PUMPUSDT", "DEADUSDT"]

    monkeypatch.setattr(MarketData, "klines", lambda self, sym, interval, total, now_ms=None: series[sym])
    monkeypatch.setattr(MarketData, "top_symbols", top)

    class Tg:
        enabled = False

    class Ctx:
        def get(self):
            return context.MarketContext()

    cfg = dict(cli.DEFAULTS, radar_interval="1h", state_file=str(tmp_path / "state.json"),
               signals_file=str(tmp_path / "signals.json"), radar_file=str(tmp_path / "radar.json"))
    assert cli.radar_once(cfg, MarketData(), Tg(), Ctx()) == 1
    radar = json.loads((tmp_path / "radar.json").read_text())
    assert radar["scanned"] == 1 and radar["rows"][0]["symbol"] == "PUMPUSDT"
    assert asked["min_volume"] == 0 and asked["count"] >= 1000        # toutes les paires
