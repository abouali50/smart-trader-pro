from smart_trader_pro import backtest, cli, compare
from smart_trader_pro.data import MarketData
from smart_trader_pro.strategy import Params
from conftest import HOUR, make_candles
from test_io import FakeResponse, kline_rows


def test_grid_and_default_variant():
    base = Params()
    vs = compare.variants(base)
    assert len(vs) == 24 and len({(repr(p), t) for p, t in vs}) == 24
    assert any(p == base and t == 2.0 for p, t in vs)
    assert compare.needed_candles(base) >= (50 + 6) * 24


def test_split_ranges_do_not_overlap_and_match_full_run(candles):
    p = Params()
    cut = 900
    train = backtest.run(candles, p, hi=cut)
    test = backtest.run(candles, p, lo=cut)
    assert train.count > 0 and test.count > 0
    assert all(t.exit_ts <= candles.ts[cut - 1] for t in train.trades)
    assert all(t.entry_ts > candles.ts[cut] for t in test.trades)
    # la partie apprentissage ne depend pas de ce qui vient apres la coupure
    shorter = make_candles(n=cut)
    again = backtest.run(shorter, p)
    assert [(t.entry_ts, round(t.r, 9)) for t in again.trades] == \
           [(t.entry_ts, round(t.r, 9)) for t in train.trades]


def test_compare_ranks_on_train_only():
    data = [make_candles(n=4000, seed=s, symbol=f"S{s}USDT") for s in (1, 2)]
    seen = []
    results = compare.run(data, None, Params(), progress=seen.append)
    assert len(results) == 24 and len(seen) == 24
    ranked = [v for v in results if v.train.count >= compare.MIN_TRAIN_TRADES]
    assert ranked == sorted(ranked, key=lambda v: v.train.expectancy, reverse=True)
    champion = compare.best(results)
    assert champion is None or champion is results[0]
    for v in results:
        assert v.train.count + v.test.count > 0


def test_holds_thresholds():
    def rep(rs):
        r = backtest.Report("X")
        r.trades = [backtest.Trade("X", i, i + 1, 1, 1, x, 1, "") for i, x in enumerate(rs)]
        return r
    good = compare.Variant(Params(), 2.0, rep([]), rep([2.0, -1.0, -1.0] * 12))
    few = compare.Variant(Params(), 2.0, rep([]), rep([2.0] * 10))
    bad = compare.Variant(Params(), 2.0, rep([]), rep([2.0, -1.0, -1.0, -1.0] * 10))
    assert not compare.holds(good)            # R moyen 0 : insuffisant
    assert not compare.holds(few) and not compare.holds(bad)
    strong = compare.Variant(Params(), 2.0, rep([]), rep([2.0, -1.0] * 20))
    assert compare.holds(strong)


def test_compare_command_runs(monkeypatch, capsys):
    c = make_candles(n=4200)
    monkeypatch.setattr(MarketData, "klines",
                        lambda self, sym, interval, total, now_ms=None: make_candles(n=4200, symbol=sym))
    monkeypatch.setattr(cli, "load_config", lambda path: dict(cli.DEFAULTS))
    cli.main(["compare", "--symbols", "AAAUSDT", "BBBUSDT", "--candles", "4200"])
    out = capsys.readouterr().out
    assert "VERDICT" in out and "VALIDATION (jamais vue)" in out and "* " in out
    assert "Variantes gagnantes sur la validation" in out
