import pytest

from smart_trader_pro import indicators as ind


def test_sma_and_ema_known_values():
    v = [1, 2, 3, 4, 5, 6]
    assert ind.sma(v, 3) == [None, None, 2, 3, 4, 5]
    e = ind.ema(v, 3)
    assert e[:2] == [None, None] and e[2] == 2
    assert e[3] == pytest.approx(3.0) and e[5] == pytest.approx(5.0)


def test_rsi_extremes_and_bounds(candles):
    assert ind.rsi(list(range(1, 40)), 14)[-1] == 100.0
    assert ind.rsi(list(range(40, 1, -1)), 14)[-1] == 0.0
    vals = [x for x in ind.rsi(candles.close) if x is not None]
    assert vals and all(0 <= x <= 100 for x in vals)


def test_atr_constant_range():
    n = 40
    highs, lows, closes = [11.0] * n, [9.0] * n, [10.0] * n
    assert ind.atr(highs, lows, closes, 14)[-1] == pytest.approx(2.0)


def test_bounded_oscillators(candles):
    c = candles
    for series in (ind.mfi(c.high, c.low, c.close, c.volume), ind.stoch_rsi(c.close),
                   ind.adx(c.high, c.low, c.close)[0]):
        vals = [x for x in series if x is not None]
        assert len(vals) > 1000 and all(-1e-9 <= x <= 100 + 1e-9 for x in vals)
    cm = [x for x in ind.cmf(c.high, c.low, c.close, c.volume) if x is not None]
    assert all(-1 <= x <= 1 for x in cm)


def test_obv_direction():
    assert ind.obv([1, 2, 2, 1], [10, 20, 30, 40]) == [0, 20, 20, -20]


def test_bollinger_contains_mid(candles):
    low, mid, up = ind.bollinger(candles.close)
    assert low[100] < mid[100] < up[100]


def test_fibonacci_levels():
    highs = [10, 11, 12, 20, 18, 17]
    lows = [9, 5, 11, 15, 16, 15]
    f = ind.fib_retracement(highs, lows, 5, window=6)
    assert f["low"] == 5 and f["high"] == 20
    assert f["0.5"] == pytest.approx(12.5)
    assert f["0.618"] == pytest.approx(20 - 0.618 * 15)
    assert f["1.618"] == pytest.approx(5 + 1.618 * 15)
    # plus haut en debut de fenetre : pas d'impulsion mesurable
    assert ind.fib_retracement([20, 11, 12], [9, 5, 11], 2, window=3) is None
