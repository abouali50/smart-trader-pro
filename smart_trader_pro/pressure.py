"""Pre-explosion : reperer une crypto sous pression AVANT qu'elle casse.

Ordre habituel d'un depart : d'abord le volume et les acheteurs, ensuite
le prix. Pendant la phase d'accumulation, le prix reste calme dans une
zone etroite alors que des acheteurs absorbent ce qui se vend. Ce module
cherche cette signature :

- compression : volatilite au plus bas, bandes de Bollinger serrees ;
- accumulation : les achats au marche dominent (delta cumule positif) et
  l'OBV monte alors que le prix n'a presque pas bouge ;
- volume qui se reveille sans que le prix parte ;
- structure : creux de plus en plus hauts, prix colle sous sa resistance ;
- force : la crypto tient mieux que le Bitcoin.

Ce n'est pas une prediction : beaucoup de zones calmes ne cassent jamais,
ou cassent vers le bas. Chaque alerte donne donc le niveau a casser et un
stop sous la zone.

Toutes les decisions a la bougie i n'utilisent que des bougies <= i.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from . import indicators as ind
from .breakout import _prior_max
from .data import Candles
from .strategy import Evaluation

DEFAULT_TP = 3.0


@dataclass
class Params:
    lookback: int = 96              # resistance : plus haut des 96 dernieres bougies
    flow_bars: int = 48             # fenetre de l'accumulation
    squeeze_window: int = 120
    squeeze_rank: float = 0.20      # largeur de Bollinger dans les 20 % les plus bas
    cvd_min: float = 0.04           # delta cumule >= +4 % du volume
    flat_pct: float = 4.0           # prix "calme" : moins de 4 % de variation
    buy_ratio: float = 0.52
    recent_bars: int = 12
    vol_build: float = 1.25         # volume recent / volume d'avant
    near_pct: float = 3.0           # a moins de 3 % sous la resistance
    max_run_pct: float = 10.0       # deja +10 % sur la fenetre : trop tard
    min_score: int = 75
    stop_lookback: int = 24
    stop_atr_buffer: float = 0.25
    stop_min_atr: float = 1.0
    stop_max_atr: float = 3.0
    tp1_r: float = 1.5
    tp2_r: float = 3.0

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Params":
        d = d or {}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Features:
    candles: Candles
    prior_high: ind.Series
    width_rank: List[Optional[float]]      # rang de la largeur de Bollinger (0 = record de calme)
    atr: ind.Series
    atr_pct_median: List[Optional[float]]
    obv: List[float]
    vol_prefix: List[float]
    buy_prefix: List[float]
    btc: Optional[Dict[int, float]]


def _width_rank(closes, window: int) -> List[Optional[float]]:
    low, mid, up = ind.bollinger(closes)
    width = [None if m is None or m == 0 else (u - l) / m for l, m, u in zip(low, mid, up)]
    out: List[Optional[float]] = [None] * len(closes)
    for i in range(len(closes)):
        start = i - window + 1
        if start < 0 or width[start] is None:
            continue
        w = width[start:i + 1]
        out[i] = sum(1 for x in w if x < width[i]) / (len(w) - 1) if len(w) > 1 else None
    return out


def _rolling_median(values, window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        start = i - window + 1
        if start < 0 or values[start] is None:
            continue
        w = sorted(values[start:i + 1])
        out[i] = w[len(w) // 2]
    return out


def prepare(c: Candles, p: Params, btc: Optional[Candles] = None) -> Features:
    atr = ind.atr(c.high, c.low, c.close)
    atr_pct = [None if a is None or cl <= 0 else a / cl for a, cl in zip(atr, c.close)]
    vp, bp = [0.0], [0.0]
    for v, b in zip(c.volume, c.taker_buy):
        vp.append(vp[-1] + v)
        bp.append(bp[-1] + b)
    return Features(
        candles=c,
        prior_high=_prior_max(c.high, p.lookback),
        width_rank=_width_rank(c.close, p.squeeze_window),
        atr=atr,
        atr_pct_median=_rolling_median(atr_pct, p.squeeze_window),
        obv=ind.obv(c.close, c.volume),
        vol_prefix=vp, buy_prefix=bp,
        btc=dict(zip(btc.ts, btc.close)) if btc is not None else None,
    )


def warmup(p: Params) -> int:
    return max(p.squeeze_window + 20, p.lookback, p.flow_bars + p.recent_bars, 60) + 4


def feature_key(p: Params) -> tuple:
    return (p.lookback, p.squeeze_window)


def market_filter(btc: Optional[Candles], p: Params) -> None:
    return None


def label(p: Params, tp_r: float) -> str:
    return (f"calme {int(p.squeeze_rank * 100):>2}% pres {p.near_pct:g}% "
            f"score {p.min_score} obj {tp_r:g}R")


def config_snippet(p: Params) -> str:
    return (f'"pressure_strategy": {{"squeeze_rank": {p.squeeze_rank}, '
            f'"near_pct": {p.near_pct}, "min_score": {p.min_score}}}')


GRID = {
    "squeeze_rank": (0.20, 0.30),
    "near_pct": (3.0, 5.0),
    "min_score": (70, 75, 80),
    "tp_r": (2.0, 3.0),
}


def evaluate(f: Features, i: int, p: Params) -> Optional[Evaluation]:
    c = f.candles
    if i < warmup(p) - 1 or i >= len(c):
        return None
    ph, rank, atr, med = f.prior_high[i], f.width_rank[i], f.atr[i], f.atr_pct_median[i]
    if ph is None or rank is None or atr is None or med is None or c.close[i] <= 0:
        return None

    close = c.close[i]
    fb, rb = p.flow_bars, p.recent_bars
    vol = f.vol_prefix[i + 1] - f.vol_prefix[i + 1 - fb]
    buy = f.buy_prefix[i + 1] - f.buy_prefix[i + 1 - fb]
    vol_recent = f.vol_prefix[i + 1] - f.vol_prefix[i + 1 - rb]
    buy_recent = f.buy_prefix[i + 1] - f.buy_prefix[i + 1 - rb]
    vol_before = f.vol_prefix[i + 1 - rb] - f.vol_prefix[i + 1 - rb - fb]
    has_flow = vol > 0 and buy > 0
    cvd = (2 * buy - vol) / vol if has_flow else 0.0
    run = (close / c.close[i - fb] - 1) * 100
    gap = (ph / close - 1) * 100                       # distance jusqu'a la resistance
    ev = Evaluation(c.symbol, c.ts[i], close, 0, 0, atr=atr)
    ev.trigger = ph

    def check(family: str, ok: bool, points: int, text: str) -> None:
        got, total = ev.families.get(family, (0, 0))
        ev.families[family] = (got + (points if ok else 0), total + points)
        if ok:
            ev.score += points
            ev.reasons.append(text)
        else:
            ev.missing.append(text)

    # Compression (25)
    check("compression", rank <= p.squeeze_rank, 15,
          f"bandes de Bollinger tres serrees (plus calme que {100 - rank * 100:.0f} % du temps)")
    check("compression", atr / close < med, 10, "volatilite sous sa moyenne recente")

    # Accumulation (30)
    check("accumulation", has_flow and cvd >= p.cvd_min, 12,
          f"achats au marche dominants : delta cumule {cvd * 100:+.0f} % du volume sur {fb} bougies")
    check("accumulation", abs(run) <= p.flat_pct and f.obv[i] > f.obv[i - fb], 10,
          f"OBV en hausse alors que le prix est calme ({run:+.1f} %)")
    ratio = buy_recent / vol_recent if vol_recent > 0 else 0.0
    check("accumulation", ratio >= p.buy_ratio, 8,
          f"acheteurs majoritaires sur les {rb} dernieres bougies ({ratio * 100:.0f} %)")

    # Volume (15)
    build = (vol_recent / rb) / (vol_before / fb) if vol_before > 0 else 0.0
    check("volume", build >= p.vol_build, 15,
          f"volume qui se reveille : x{build:.1f} sans que le prix parte")

    # Structure (20)
    check("structure", 0 <= gap <= p.near_pct, 12,
          f"prix colle sous sa resistance (a {gap:.1f} %)")
    low_now = min(c.low[i - rb + 1:i + 1])
    low_prev = min(c.low[i - 2 * rb + 1:i - rb + 1])
    check("structure", low_now > low_prev, 8, "creux de plus en plus hauts")

    # Force (10)
    rs_ok, rs_text = False, "tient mieux que le BTC"
    if f.btc is not None:
        b_now, b_then = f.btc.get(c.ts[i]), f.btc.get(c.ts[i - p.lookback])
        if b_now and b_then:
            diff = (close / c.close[i - p.lookback] - b_now / b_then) * 100
            rs_ok = diff >= 0
            rs_text = f"ecart avec le BTC sur {p.lookback} bougies : {diff:+.1f} %"
    check("force", rs_ok, 10, rs_text)

    not_started = close <= ph and run < p.max_run_pct
    if not_started and ev.score >= p.min_score and atr > 0:
        lo = max(0, i - p.stop_lookback + 1)
        base = min(c.low[lo:i + 1])
        dist = close - (base - p.stop_atr_buffer * atr)
        dist = min(max(dist, p.stop_min_atr * atr), p.stop_max_atr * atr)
        ev.stop = close - dist
        ev.tp1 = close + p.tp1_r * dist
        ev.tp2 = close + p.tp2_r * dist
        ev.is_signal = True
    return ev
