"""Comparaison honnete de variantes de la strategie.

Methode : l'historique est coupe en deux dans le temps. Chaque variante
est mesuree sur la premiere partie (apprentissage). Celle qui y fait le
mieux est ensuite jugee sur la seconde partie (validation), qu'elle n'a
jamais vue. Seul ce second resultat compte : choisir une variante parce
qu'elle brille sur des donnees deja vues, c'est ajuster le passe.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
from typing import Callable, Dict, List, Optional

from . import backtest
from . import strategy as _strategy
from .data import Candles

MIN_TRAIN_TRADES = 30
MIN_TEST_TRADES = 30
MIN_TEST_EXPECTANCY = 0.10
MIN_TEST_PF = 1.15


@dataclass
class Variant:
    params: object
    tp_r: float
    train: backtest.Report
    test: backtest.Report
    engine: object = _strategy

    @property
    def label(self) -> str:
        return self.engine.label(self.params, self.tp_r)

    def is_default(self, base) -> bool:
        return self.params == base and self.tp_r == self.engine.DEFAULT_TP


def variants(base, engine=_strategy) -> List[tuple]:
    keys = list(engine.GRID)
    out = []
    for values in product(*(engine.GRID[k] for k in keys)):
        v = dict(zip(keys, values))
        tp_r = v.pop("tp_r")
        out.append((replace(base, **v), tp_r))
    return out


def split_index(c: Candles, train_ratio: float) -> int:
    return int(len(c) * train_ratio)


def run(candles: List[Candles], btc: Optional[Candles], base,
        train_ratio: float = 0.6, fee_pct: float = 0.1, max_bars: int = 72,
        progress: Optional[Callable[[str], None]] = None,
        engine=_strategy) -> List[Variant]:
    """Mesure chaque variante sur les deux parties. `btc` sert au filtre de marche
    (repli) ou a la force relative (radar)."""
    feats: Dict[tuple, object] = {}
    market: Dict[tuple, Optional[Dict[int, int]]] = {}
    results: List[Variant] = []
    for params, tp_r in variants(base, engine):
        fk = engine.feature_key(params)
        if fk not in market:
            market[fk] = engine.market_filter(btc, params)
        train, test = [], []
        for c in candles:
            key = (c.symbol,) + fk
            if key not in feats:
                feats[key] = engine.prepare(c, params, btc=btc)
            cut = split_index(c, train_ratio)
            common = dict(tp_r=tp_r, max_bars=max_bars, fee_pct=fee_pct,
                          market_trend=market[fk], features=feats[key], engine=engine)
            train.append(backtest.run(c, params, hi=cut, **common))
            test.append(backtest.run(c, params, lo=cut, **common))
        v = Variant(params, tp_r, backtest.merge(train), backtest.merge(test), engine)
        results.append(v)
        if progress:
            progress(v.label)
    results.sort(key=lambda v: (v.train.count >= MIN_TRAIN_TRADES, v.train.expectancy),
                 reverse=True)
    return results


def best(results: List[Variant]) -> Optional[Variant]:
    """La meilleure variante sur l'apprentissage, si elle a assez de trades."""
    if results and results[0].train.count >= MIN_TRAIN_TRADES:
        return results[0]
    return None


def holds(v: Variant) -> bool:
    t = v.test
    return (t.count >= MIN_TEST_TRADES and t.expectancy >= MIN_TEST_EXPECTANCY
            and t.profit_factor >= MIN_TEST_PF)


def needed_candles(base, engine=_strategy) -> int:
    """Historique minimal pour que les deux parties aient de la matiere."""
    return max(engine.warmup(p) for p, _ in variants(base, engine)) + 2000
