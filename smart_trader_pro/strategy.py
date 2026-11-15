"""Strategie : repli dans une tendance haussiere, notee par confluence.

Idee : n'acheter que dans le sens de la tendance de l'unite de temps
superieure, apres un repli vers les moyennes mobiles, quand le prix
repart avec momentum et volume. Chaque condition rapporte des points ;
un signal exige la tendance de fond ET un score minimal.

Toutes les decisions a la bougie i n'utilisent que des bougies <= i.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from . import indicators as ind
from .data import Candles, INTERVAL_MS


@dataclass
class Params:
    htf_factor: int = 4          # 1h -> tendance lue en 4h
    ema_fast: int = 21
    ema_mid: int = 50
    ema_slow: int = 200
    htf_ema: int = 50
    htf_slope_bars: int = 5
    rsi_period: int = 14
    atr_period: int = 14
    vol_period: int = 20
    vol_mult: float = 1.3
    lookback: int = 5
    rsi_dip: float = 45.0
    rsi_cap: float = 68.0
    max_extension_atr: float = 1.5
    adx_min: float = 20.0
    fib_window: int = 60
    fib_min_atr: float = 3.0
    obv_ema: int = 20
    mfi_cap: float = 80.0
    min_score: int = 70
    stop_lookback: int = 10
    stop_atr_buffer: float = 0.25
    stop_min_atr: float = 1.0
    stop_max_atr: float = 3.0
    tp1_r: float = 1.5
    tp2_r: float = 3.0

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Params":
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Features:
    candles: Candles
    ema_fast: ind.Series
    ema_mid: ind.Series
    ema_slow: ind.Series
    rsi: ind.Series
    atr: ind.Series
    hist: ind.Series
    vol_sma: ind.Series
    bb_low: ind.Series
    bb_mid: ind.Series
    obv: List[float]
    obv_ema: ind.Series
    mfi: ind.Series
    cmf: ind.Series
    stoch: ind.Series
    adx: ind.Series
    pdi: ind.Series
    mdi: ind.Series
    htf_at: List[int]            # indice de la derniere bougie HTF cloturee
    htf_close: List[float]
    htf_ema: ind.Series

    def trend(self, i: int, slope_bars: int) -> int:
        """+1 haussier, -1 baissier, 0 neutre ou inconnu."""
        j = self.htf_at[i]
        if j < slope_bars:
            return 0
        e, e_prev = self.htf_ema[j], self.htf_ema[j - slope_bars]
        if e is None or e_prev is None:
            return 0
        c = self.htf_close[j]
        if c > e and e > e_prev:
            return 1
        if c < e and e < e_prev:
            return -1
        return 0


@dataclass
class Evaluation:
    symbol: str
    ts: int
    price: float
    score: int
    trend: int
    reasons: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    is_signal: bool = False
    stop: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    atr: Optional[float] = None
    families: dict = field(default_factory=dict)   # points par famille
    fib: Optional[dict] = None

    @property
    def risk_pct(self) -> Optional[float]:
        if self.stop is None:
            return None
        return (self.price - self.stop) / self.price * 100


def _higher_timeframe(c: Candles, factor: int):
    """Regroupe les bougies par blocs alignes sur l'horloge.

    Renvoie (htf_at, htf_close) ou htf_at[i] est l'indice du dernier bloc
    entierement cloture a la fermeture de la bougie i (-1 si aucun).
    """
    step = INTERVAL_MS[c.interval]
    span = step * factor
    htf_close: List[float] = []
    htf_at: List[int] = []
    group_start: Optional[int] = None
    for i, t in enumerate(c.ts):
        g = t - (t % span)
        if g != group_start:
            group_start = g
        if t + step == group_start + span:       # derniere bougie du bloc
            htf_close.append(c.close[i])
        htf_at.append(len(htf_close) - 1)
    return htf_at, htf_close


def prepare(c: Candles, p: Params, btc: Optional[Candles] = None) -> Features:
    htf_at, htf_close = _higher_timeframe(c, p.htf_factor)
    bb_low, bb_mid, _ = ind.bollinger(c.close)
    obv = ind.obv(c.close, c.volume)
    adx, pdi, mdi = ind.adx(c.high, c.low, c.close)
    return Features(
        bb_low=bb_low, bb_mid=bb_mid, obv=obv, obv_ema=ind.ema(obv, p.obv_ema),
        mfi=ind.mfi(c.high, c.low, c.close, c.volume),
        cmf=ind.cmf(c.high, c.low, c.close, c.volume),
        stoch=ind.stoch_rsi(c.close), adx=adx, pdi=pdi, mdi=mdi,
        candles=c,
        ema_fast=ind.ema(c.close, p.ema_fast),
        ema_mid=ind.ema(c.close, p.ema_mid),
        ema_slow=ind.ema(c.close, p.ema_slow),
        rsi=ind.rsi(c.close, p.rsi_period),
        atr=ind.atr(c.high, c.low, c.close, p.atr_period),
        hist=ind.macd_hist(c.close),
        vol_sma=ind.sma(c.volume, p.vol_period),
        htf_at=htf_at,
        htf_close=htf_close,
        htf_ema=ind.ema(htf_close, p.htf_ema),
    )


DEFAULT_TP = 2.0

GRID = {
    "htf_factor": (4, 24),          # tendance lue en 4 h ou a la journee
    "min_score": (70, 80),
    "stop_min_atr": (1.0, 2.0),
    "tp_r": (1.5, 2.0, 3.0),
}


def feature_key(p: Params) -> tuple:
    """Parametres dont dependent les indicateurs precalcules."""
    return (p.htf_factor,)


def market_filter(btc: Optional[Candles], p: Params) -> Optional[dict]:
    """Tendance de fond du BTC par horodatage ; -1 bloque les entrees."""
    if btc is None:
        return None
    fb = prepare(btc, p)
    return {btc.ts[i]: fb.trend(i, p.htf_slope_bars) for i in range(len(btc))}


def label(p: Params, tp_r: float) -> str:
    fond = "jour" if p.htf_factor == 24 else f"{p.htf_factor}h"
    return f"fond {fond:<4} score {p.min_score} stop {p.stop_min_atr:g}ATR obj {tp_r:g}R"


def config_snippet(p: Params) -> str:
    return (f'"strategy": {{"htf_factor": {p.htf_factor}, "min_score": {p.min_score}, '
            f'"stop_min_atr": {p.stop_min_atr}}}')


def warmup(p: Params) -> int:
    """Nombre de bougies necessaires avant la premiere evaluation fiable."""
    return max(p.ema_slow, p.fib_window,
               (p.htf_ema + p.htf_slope_bars + 1) * p.htf_factor) + p.lookback + 1


def evaluate(f: Features, i: int, p: Params) -> Optional[Evaluation]:
    c = f.candles
    if i < p.lookback + 1 or i >= len(c):
        return None
    needed = (f.ema_fast[i], f.ema_mid[i], f.ema_slow[i], f.rsi[i], f.rsi[i - 1],
              f.atr[i], f.hist[i], f.hist[i - 1], f.vol_sma[i - 1],
              f.ema_fast[i - p.lookback], f.rsi[i - p.lookback],
              f.bb_mid[i - p.lookback], f.obv_ema[i], f.mfi[i - 1], f.cmf[i],
              f.stoch[i - 1], f.adx[i])
    if any(v is None for v in needed):
        return None

    close, atr = c.close[i], f.atr[i]
    e21, e200 = f.ema_fast[i], f.ema_slow[i]
    trend = f.trend(i, p.htf_slope_bars)
    ev = Evaluation(c.symbol, c.ts[i], close, 0, trend, atr=atr)

    def check(family: str, ok: bool, points: int, label: str) -> None:
        got, total = ev.families.get(family, (0, 0))
        ev.families[family] = (got + (points if ok else 0), total + points)
        if ok:
            ev.score += points
            ev.reasons.append(label)
        else:
            ev.missing.append(label)

    window = range(i - p.lookback + 1, i + 1)
    recent_low = min(c.low[k] for k in window)
    bullish = close > c.open[i]

    # Tendance (30)
    check("tendance", trend == 1, 20, "tendance de fond haussiere")
    check("tendance", close > e200, 5, "prix au-dessus de l'EMA200")
    check("tendance", f.adx[i] >= p.adx_min and f.pdi[i] > f.mdi[i], 5,
          "ADX : tendance forte et acheteuse")

    # Structure du repli (25)
    touched = any(c.low[k] <= f.ema_fast[k] for k in window)
    check("structure", touched and close > e21 and bullish, 10,
          "repli sur l'EMA21 puis reprise")
    fib = ind.fib_retracement(c.high, c.low, i, p.fib_window)
    in_zone = False
    if fib and fib["high"] - fib["low"] >= p.fib_min_atr * atr:
        ev.fib = fib
        in_zone = fib["0.618"] <= recent_low <= fib["0.382"] and close > fib["0.618"]
    check("structure", in_zone, 10, "repli dans la zone Fibonacci 0.382-0.618")
    bb_touch = any(c.low[k] <= f.bb_mid[k] for k in window)
    check("structure", bb_touch and bullish, 5, "rebond sur la mediane de Bollinger")

    # Momentum (20)
    dipped = min(f.rsi[k] for k in window) < p.rsi_dip
    check("momentum", dipped and f.rsi[i] > f.rsi[i - 1] and f.rsi[i] < p.rsi_cap, 8,
          "RSI qui remonte apres un creux")
    check("momentum", f.hist[i] > f.hist[i - 1], 4, "momentum MACD en hausse")
    check("momentum", f.hist[i] > 0 >= f.hist[i - 1], 4, "croisement haussier du MACD")
    check("momentum", f.stoch[i] > f.stoch[i - 1] and f.stoch[i - 1] < 30, 4,
          "RSI stochastique qui sort de la survente")

    # Volume (20)
    check("volume", c.volume[i] > p.vol_mult * f.vol_sma[i - 1], 6,
          "volume superieur a la moyenne")
    check("volume", f.obv[i] > f.obv_ema[i], 5, "OBV en accumulation")
    check("volume", f.cmf[i] > 0, 5, "flux d'argent positif (CMF)")
    check("volume", f.mfi[i] > f.mfi[i - 1] and f.mfi[i] < p.mfi_cap, 4,
          "MFI en hausse sans surachat")

    # Risque (5)
    check("risque", close - e21 < p.max_extension_atr * atr, 5, "prix non surchauffe")

    if trend == 1 and ev.score >= p.min_score and atr > 0:
        lo = max(0, i - p.stop_lookback + 1)
        swing = min(c.low[lo:i + 1])
        dist = close - (swing - p.stop_atr_buffer * atr)
        dist = min(max(dist, p.stop_min_atr * atr), p.stop_max_atr * atr)
        ev.stop = close - dist
        ev.tp1 = close + p.tp1_r * dist
        ev.tp2 = close + p.tp2_r * dist
        ev.is_signal = True
    return ev


def position_size(capital: float, risk_pct: float, entry: float, stop: float):
    """Quantite telle qu'un stop touche coute `risk_pct` % du capital.

    Renvoie (quantite, montant engage). Le montant ne depasse jamais le capital.
    """
    if entry <= stop or capital <= 0 or risk_pct <= 0:
        return 0.0, 0.0
    qty = capital * risk_pct / 100 / (entry - stop)
    qty = min(qty, capital / entry)
    return qty, qty * entry
