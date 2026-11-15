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
from .data import Candles
from .strategy import Params, prepare, warmup

GRID = {
    "htf_factor": (4, 24),          # tendance lue en 4 h ou a la journee
    "min_score": (70, 80),
    "stop_min_atr": (1.0, 2.0),
    "tp_r": (1.5, 2.0, 3.0),
}

MIN_TRAIN_TRADES = 30
MIN_TEST_TRADES = 30
MIN_TEST_EXPECTANCY = 0.10
MIN_TEST_PF = 1.15


@dataclass
class Variant:
    params: Params
    tp_r: float
    train: backtest.Report
    test: backtest.Report

    @property
    def label(self) -> str:
        fond = "jour" if self.params.htf_factor == 24 else f"{self.params.htf_factor}h"
        return (f"fond {fond:<4} score {self.params.min_score} "
                f"stop {self.params.stop_min_atr:g}ATR obj {self.tp_r:g}R")

    def is_default(self, base: Params) -> bool:
        return self.params == base and self.tp_r == 2.0


def variants(base: Params) -> List[tuple]:
    keys = list(GRID)
    out = []
    for values in product(*(GRID[k] for k in keys)):
        v = dict(zip(keys, values))
        tp_r = v.pop("tp_r")
        out.append((replace(base, **v), tp_r))
    return out


def split_index(c: Candles, train_ratio: float) -> int:
    return int(len(c) * train_ratio)


def run(candles: List[Candles], btc: Optional[Candles], base: Params,
        train_ratio: float = 0.6, fee_pct: float = 0.1, max_bars: int = 72,
        progress: Optional[Callable[[str], None]] = None) -> List[Variant]:
    """Mesure chaque variante sur les deux parties. `btc` active le filtre BTC."""
    feats: Dict[tuple, object] = {}
    market: Dict[int, Optional[Dict[int, int]]] = {}
    results: List[Variant] = []
    for params, tp_r in variants(base):
        hf = params.htf_factor
        if hf not in market:
            market[hf] = None
            if btc is not None:
                fb = prepare(btc, params)
                market[hf] = {btc.ts[i]: fb.trend(i, params.htf_slope_bars)
                              for i in range(len(btc))}
        train, test = [], []
        for c in candles:
            key = (c.symbol, hf)
            if key not in feats:
                feats[key] = prepare(c, params)
            cut = split_index(c, train_ratio)
            common = dict(tp_r=tp_r, max_bars=max_bars, fee_pct=fee_pct,
                          market_trend=market[hf], features=feats[key])
            train.append(backtest.run(c, params, hi=cut, **common))
            test.append(backtest.run(c, params, lo=cut, **common))
        v = Variant(params, tp_r, backtest.merge(train), backtest.merge(test))
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


def needed_candles(base: Params) -> int:
    """Historique minimal pour que les deux parties aient de la matiere."""
    return max(warmup(p) for p, _ in variants(base)) + 2000
