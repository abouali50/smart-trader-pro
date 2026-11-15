"""Backtest de la strategie sur l'historique, avec hypotheses prudentes.

- entree a l'ouverture de la bougie qui suit le signal ;
- si le stop et l'objectif sont touches dans la meme bougie, on compte le stop ;
- ouverture sous le stop : sortie au prix d'ouverture (perte > 1R) ;
- frais appliques a l'entree et a la sortie.

Un backtest decrit le passe ; il ne garantit rien pour la suite.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .data import Candles
from . import strategy as _strategy


@dataclass
class Trade:
    symbol: str
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    r: float            # resultat net en multiples du risque
    bars: int
    outcome: str        # "objectif", "stop", "temps"


@dataclass
class Report:
    symbol: str
    trades: List[Trade] = field(default_factory=list)
    buy_hold_pct: float = 0.0

    @property
    def count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        return 100 * sum(t.r > 0 for t in self.trades) / self.count if self.count else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.r for t in self.trades)

    @property
    def expectancy(self) -> float:
        return self.total_r / self.count if self.count else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.r for t in self.trades if t.r > 0)
        losses = -sum(t.r for t in self.trades if t.r < 0)
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def max_drawdown_r(self) -> float:
        peak = equity = worst = 0.0
        for t in sorted(self.trades, key=lambda t: t.exit_ts):
            equity += t.r
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        return worst

    def return_pct(self, risk_pct: float) -> float:
        """Rendement compose en risquant `risk_pct` % du capital par trade."""
        equity = 1.0
        for t in sorted(self.trades, key=lambda t: t.exit_ts):
            equity *= 1 + t.r * risk_pct / 100
        return (equity - 1) * 100


def run(c: Candles, p, tp_r: float = 2.0, max_bars: int = 72,
        fee_pct: float = 0.1, market_trend: Optional[Dict[int, int]] = None,
        features=None, lo: Optional[int] = None, hi: Optional[int] = None,
        engine=_strategy) -> Report:
    """Backtest sur les bougies [lo, hi) ; par defaut tout l'historique.

    `market_trend` : horodatage -> tendance du BTC ; -1 bloque les entrees.
    `features` : indicateurs deja calcules (ils sont causaux, donc decouper
    l'historique apres coup ne fait fuiter aucune information future).
    Toute position ouverte est soldee a la derniere bougie de la plage.
    """
    evaluate = getattr(engine, "evaluate_fast", engine.evaluate)
    f = features if features is not None else engine.prepare(c, p)
    rep = Report(c.symbol)
    n = len(c) if hi is None else min(hi, len(c))
    start = max(engine.warmup(p), lo or 0)
    if n <= start + 1:
        return rep
    rep.buy_hold_pct = (c.close[n - 1] / c.open[start + 1] - 1) * 100
    fee = fee_pct / 100
    i = start
    while i < n - 1:
        ev = evaluate(f, i, p)
        blocked = market_trend is not None and market_trend.get(c.ts[i], 0) == -1
        if ev is None or not ev.is_signal or blocked:
            i += 1
            continue
        dist = ev.price - ev.stop
        entry = c.open[i + 1]
        stop, target = entry - dist, entry + tp_r * dist
        j = i + 1
        while True:
            if c.open[j] <= stop and j > i + 1:
                price, outcome = c.open[j], "stop"
            elif c.low[j] <= stop:
                price, outcome = stop, "stop"
            elif c.high[j] >= target:
                price, outcome = target, "objectif"
            elif j - (i + 1) >= max_bars or j == n - 1:
                price, outcome = c.close[j], "temps"
            else:
                j += 1
                continue
            break
        r = (price - entry - (entry + price) * fee) / dist
        rep.trades.append(Trade(c.symbol, c.ts[i + 1], c.ts[j], entry, price, r,
                                j - i, outcome))
        i = j
    return rep


def merge(reports: List[Report]) -> Report:
    total = Report("TOTAL")
    for r in reports:
        total.trades.extend(r.trades)
    if reports:
        total.buy_hold_pct = sum(r.buy_hold_pct for r in reports) / len(reports)
    return total
