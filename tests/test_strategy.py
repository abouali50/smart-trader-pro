import copy

from smart_trader_pro import backtest
from smart_trader_pro.strategy import (Params, _higher_timeframe, evaluate, position_size,
                                  prepare, warmup)
from conftest import make_candles


def test_higher_timeframe_only_uses_closed_blocks(candles):
    htf_at, htf_close = _higher_timeframe(candles, 4)
    assert htf_at[:3] == [-1, -1, -1] and htf_at[3] == 0 and htf_at[6] == 0 and htf_at[7] == 1
    assert htf_close[0] == candles.close[3] and htf_close[1] == candles.close[7]


def test_no_lookahead(candles):
    """Modifier le futur ne doit pas changer une evaluation passee."""
    p = Params()
    i = 900
    before = evaluate(prepare(candles, p), i, p)
    altered = copy.deepcopy(candles)
    for k in range(i + 1, len(altered)):
        for name in ("open", "high", "low", "close", "volume"):
            getattr(altered, name)[k] *= 3
    after = evaluate(prepare(altered, p), i, p)
    assert before is not None
    assert (before.score, before.reasons, before.stop) == (after.score, after.reasons, after.stop)
    truncated = copy.deepcopy(candles)
    for name in ("ts", "open", "high", "low", "close", "volume"):
        setattr(truncated, name, getattr(truncated, name)[:i + 1])
    cut = evaluate(prepare(truncated, p), i, p)
    assert (before.score, before.stop) == (cut.score, cut.stop)


def test_scores_and_signal_levels(candles):
    p = Params()
    f = prepare(candles, p)
    signals = 0
    for i in range(warmup(p), len(candles)):
        ev = evaluate(f, i, p)
        assert ev is not None and 0 <= ev.score <= 100
        assert sum(t for _, t in ev.families.values()) == 100
        assert sum(g for g, _ in ev.families.values()) == ev.score
        if ev.is_signal:
            signals += 1
            assert ev.trend == 1 and ev.score >= p.min_score
            assert ev.stop < ev.price < ev.tp1 < ev.tp2
            dist = ev.price - ev.stop
            assert p.stop_min_atr * ev.atr - 1e-9 <= dist <= p.stop_max_atr * ev.atr + 1e-9
    assert signals > 0


def test_downtrend_gives_no_signal():
    c = make_candles(drift=-0.002, seed=3)
    p = Params()
    f = prepare(c, p)
    late = [evaluate(f, i, p) for i in range(len(c) - 300, len(c))]
    assert not any(ev.is_signal for ev in late if ev)


def test_position_size():
    qty, notional = position_size(1000, 1, 100, 98)
    assert qty == 5 and notional == 500          # perte au stop : 5 x 2 = 10 = 1 %
    qty, notional = position_size(1000, 1, 100, 99.9)
    assert notional == 1000                       # plafonne au capital
    assert position_size(1000, 1, 100, 101) == (0.0, 0.0)


def test_backtest_accounting(candles):
    p = Params()
    rep = backtest.run(candles, p, tp_r=2.0, fee_pct=0.1)
    assert rep.count > 0
    last_exit = 0
    for t in rep.trades:
        assert t.entry_ts >= last_exit            # jamais deux positions a la fois
        last_exit = t.exit_ts
        assert t.r <= 2.0                         # frais deduits : jamais plus que l'objectif
        if t.outcome == "stop":
            assert t.r < -1.0                     # stop + frais
    free = backtest.run(candles, p, tp_r=2.0, fee_pct=0.0)
    assert free.total_r > rep.total_r
    blocked = backtest.run(candles, p, market_trend={t: -1 for t in candles.ts})
    assert blocked.count == 0
