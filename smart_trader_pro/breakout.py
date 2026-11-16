"""Radar d'explosion : reperer les cryptos qui demarrent.

Idee : un mouvement violent commence presque toujours par une cassure
(le prix sort par le haut d'une zone ou il etait enferme) accompagnee
d'un volume tres superieur a l'habitude. Le radar cherche ce moment sur
un grand nombre de paires et note sa qualite.

Il ne predit pas l'avenir : il detecte un depart. Beaucoup de cassures
echouent, c'est pourquoi chaque signal porte un stop.

Toutes les decisions a la bougie i n'utilisent que des bougies <= i.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import indicators as ind
from .data import Candles
from .strategy import Evaluation

DEFAULT_TP = 3.0


@dataclass
class Params:
    breakout_lookback: int = 48     # plus haut des 48 dernieres bougies
    vol_window: int = 20
    vol_mult: float = 3.0           # volume de la bougie / moyenne
    surge_bars: int = 4             # volume cumule recent
    surge_base: int = 80
    surge_mult: float = 2.0
    squeeze_window: int = 120
    squeeze_recent: int = 12
    squeeze_pct: float = 0.25       # compression : largeur de Bollinger dans le quart bas
    buy_ratio_bar: float = 0.60     # part du volume achetee au marche (bougie)
    buy_ratio_recent: float = 0.55  # idem sur les dernieres bougies
    rsi_min: float = 55.0
    rs_bars: int = 24               # force relative face au BTC
    rs_min_pct: float = 3.0
    late_bars: int = 24
    late_pct: float = 15.0          # deja +15 % : trop tard
    min_score: int = 70
    market_bars: int = 0            # filtre de marche : moyenne du BTC sur N bougies (0 = sans)
    stop_lookback: int = 12
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
    prior_high: ind.Series          # plus haut des N bougies precedentes
    vol_sma: ind.Series
    surge: ind.Series               # volume recent / volume habituel
    obv: List[float]
    obv_prior_high: ind.Series
    squeezed: List[bool]
    rsi: ind.Series
    hist: ind.Series
    adx: ind.Series
    pdi: ind.Series
    mdi: ind.Series
    atr: ind.Series
    ema_mid: ind.Series
    btc: Optional[Dict[int, float]]


def _prior_max(values, window: int) -> ind.Series:
    """Maximum des `window` valeurs precedentes (la valeur courante exclue)."""
    out: ind.Series = [None] * len(values)
    dq: deque = deque()
    for i, v in enumerate(values):
        if i >= window:
            while dq and dq[0] < i - window:
                dq.popleft()
            out[i] = values[dq[0]]
        while dq and values[dq[-1]] <= v:
            dq.pop()
        dq.append(i)
    return out


def _surge(volumes, bars: int, base: int, ) -> ind.Series:
    out: ind.Series = [None] * len(volumes)
    prefix = [0.0]
    for v in volumes:
        prefix.append(prefix[-1] + v)
    for i in range(bars + base - 1, len(volumes)):
        recent = prefix[i + 1] - prefix[i + 1 - bars]
        usual = (prefix[i + 1 - bars] - prefix[i + 1 - bars - base]) / base * bars
        out[i] = recent / usual if usual > 0 else None
    return out


def _squeezed(closes, window: int, recent: int, pct: float) -> List[bool]:
    """Vrai si la largeur de Bollinger a ete dans le bas de sa plage recemment."""
    low, mid, up = ind.bollinger(closes)
    width = [None if m is None or m == 0 else (u - l) / m for l, m, u in zip(low, mid, up)]
    out = [False] * len(closes)
    for i in range(len(closes)):
        start = i - window + 1
        if start < 0 or width[start] is None:
            continue
        w = sorted(width[start:i + 1])
        threshold = w[int((len(w) - 1) * pct)]
        out[i] = min(width[i - recent + 1:i + 1]) <= threshold
    return out


def prepare(c: Candles, p: Params, btc: Optional[Candles] = None) -> Features:
    obv = ind.obv(c.close, c.volume)
    adx, pdi, mdi = ind.adx(c.high, c.low, c.close)
    return Features(
        candles=c,
        prior_high=_prior_max(c.high, p.breakout_lookback),
        vol_sma=ind.sma(c.volume, p.vol_window),
        surge=_surge(c.volume, p.surge_bars, p.surge_base),
        obv=obv,
        obv_prior_high=_prior_max(obv, p.breakout_lookback),
        squeezed=_squeezed(c.close, p.squeeze_window, p.squeeze_recent, p.squeeze_pct),
        rsi=ind.rsi(c.close), hist=ind.macd_hist(c.close),
        adx=adx, pdi=pdi, mdi=mdi,
        atr=ind.atr(c.high, c.low, c.close),
        ema_mid=ind.ema(c.close, 50),
        btc=dict(zip(btc.ts, btc.close)) if btc is not None else None,
    )


def warmup(p: Params) -> int:
    return max(p.squeeze_window + 20, p.breakout_lookback, p.surge_bars + p.surge_base,
               p.rs_bars, p.late_bars, 60) + 4


def feature_key(p: Params) -> tuple:
    """Parametres dont dependent les indicateurs precalcules."""
    return (p.breakout_lookback, p.vol_window, p.surge_bars, p.surge_base,
            p.squeeze_window, p.squeeze_recent, p.squeeze_pct)


def market_states(btc: Optional[Candles], p: Params) -> List[int]:
    """Etat du marche a chaque bougie du BTC : 1 haussier, -1 baissier, 0 inconnu.

    Haussier = le BTC cloture au-dessus de sa moyenne exponentielle sur
    `market_bars` bougies et cette moyenne monte (sur un septieme de la periode).
    """
    if btc is None or p.market_bars <= 0:
        return []
    ema = ind.ema(btc.close, p.market_bars)
    slope = max(1, p.market_bars // 7)
    out = [0] * len(btc)
    for i in range(len(btc)):
        if i < slope or ema[i] is None or ema[i - slope] is None:
            continue
        out[i] = 1 if (btc.close[i] > ema[i] and ema[i] > ema[i - slope]) else -1
    return out


def market_filter(btc: Optional[Candles], p: Params) -> Optional[Dict[int, int]]:
    """Etat du marche par horodatage ; -1 bloque les entrees. None = pas de filtre."""
    states = market_states(btc, p)
    return dict(zip(btc.ts, states)) if states else None


def market_now(btc: Optional[Candles], p: Params) -> Optional[str]:
    """"up", "down", ou None si le filtre est coupe ou l'historique trop court."""
    states = market_states(btc, p)
    if not states or states[-1] == 0:
        return None
    return "up" if states[-1] == 1 else "down"


def market_need(p: Params) -> int:
    """Bougies du BTC necessaires pour que la moyenne soit stabilisee."""
    return p.market_bars * 3 + max(1, p.market_bars // 7) if p.market_bars > 0 else 0


def label(p: Params, tp_r: float) -> str:
    return (f"haut {p.breakout_lookback:>2}b vol x{p.vol_mult:g} "
            f"score {p.min_score} obj {tp_r:g}R" + (" BTC+" if p.market_bars > 0 else ""))


def config_snippet(p: Params) -> str:
    return (f'"radar_strategy": {{"breakout_lookback": {p.breakout_lookback}, '
            f'"vol_mult": {p.vol_mult}, "min_score": {p.min_score}, '
            f'"market_bars": {p.market_bars}}}')


GRID = {
    "breakout_lookback": (24, 48, 96),
    "vol_mult": (2.0, 3.0),
    "min_score": (60, 70, 80),
    "market_bars": (0, 672),         # sans filtre / BTC haussier sur 7 jours (en 15 minutes)
    "tp_r": (2.0, 3.0),
}


def _is_breakout(f: Features, i: int) -> bool:
    ph = f.prior_high[i]
    return ph is not None and f.candles.close[i] > ph


def evaluate_fast(f: Features, i: int, p: Params) -> Optional[Evaluation]:
    """Pour le backtest : n'evalue en detail que les bougies de cassure."""
    if not _is_breakout(f, i):
        return None
    return evaluate(f, i, p)


def evaluate(f: Features, i: int, p: Params) -> Optional[Evaluation]:
    c = f.candles
    if i < warmup(p) - 1 or i >= len(c):
        return None
    needed = (f.prior_high[i], f.vol_sma[i - 1], f.surge[i], f.obv_prior_high[i],
              f.rsi[i], f.rsi[i - 1], f.hist[i], f.hist[i - 1], f.adx[i], f.adx[i - 3],
              f.atr[i], f.ema_mid[i])
    if any(v is None for v in needed):
        return None

    close, atr = c.close[i], f.atr[i]
    breakout = close > f.prior_high[i]
    ev = Evaluation(c.symbol, c.ts[i], close, 0, 1 if close > f.ema_mid[i] else -1, atr=atr)

    def check(family: str, ok: bool, points: int, text: str) -> None:
        got, total = ev.families.get(family, (0, 0))
        ev.families[family] = (got + (points if ok else 0), total + points)
        if ok:
            ev.score += points
            ev.reasons.append(text)
        else:
            ev.missing.append(text)

    # Volume (30)
    ratio = c.volume[i] / f.vol_sma[i - 1] if f.vol_sma[i - 1] > 0 else 0.0
    check("volume", ratio >= p.vol_mult, 17,
          f"volume de la bougie x{ratio:.1f} (seuil x{p.vol_mult:g})")
    check("volume", f.surge[i] >= p.surge_mult, 8,
          f"volume des {p.surge_bars} dernieres bougies x{f.surge[i]:.1f}")
    check("volume", f.obv[i] > f.obv_prior_high[i], 5, "OBV au plus haut : accumulation")

    # Acheteurs (10) : qui paie le prix du marche pour entrer tout de suite
    bar_buy = c.taker_buy[i] / c.volume[i] if c.volume[i] > 0 else 0.0
    lo_b = i - p.surge_bars + 1
    vol_recent = sum(c.volume[lo_b:i + 1])
    recent_buy = sum(c.taker_buy[lo_b:i + 1]) / vol_recent if vol_recent > 0 else 0.0
    check("acheteurs", bar_buy >= p.buy_ratio_bar, 6,
          f"achats au marche : {bar_buy * 100:.0f} % du volume de la bougie")
    check("acheteurs", recent_buy >= p.buy_ratio_recent, 4,
          f"pression acheteuse sur {p.surge_bars} bougies : {recent_buy * 100:.0f} %")

    # Cassure (30)
    check("cassure", breakout, 20,
          f"cloture au-dessus du plus haut des {p.breakout_lookback} dernieres bougies")
    check("cassure", f.squeezed[i], 10, "sortie d'une phase de compression (Bollinger)")

    # Momentum (15)
    check("momentum", f.rsi[i] >= p.rsi_min and f.rsi[i] > f.rsi[i - 1], 5,
          "RSI fort et en hausse")
    check("momentum", f.hist[i] > 0 and f.hist[i] > f.hist[i - 1], 4,
          "MACD positif et en hausse")
    check("momentum", f.adx[i] > f.adx[i - 3] and f.pdi[i] > f.mdi[i], 3,
          "ADX en hausse, acheteurs dominants")
    rng = c.high[i] - c.low[i]
    check("momentum", rng > 0 and (close - c.low[i]) / rng >= 0.75, 3,
          "cloture pres du plus haut de la bougie")

    # Force relative (10)
    rs_ok, rs_text = False, f"plus forte que le BTC sur {p.rs_bars} bougies"
    if f.btc is not None:
        b_now, b_then = f.btc.get(c.ts[i]), f.btc.get(c.ts[i - p.rs_bars])
        if b_now and b_then:
            diff = (close / c.close[i - p.rs_bars] - b_now / b_then) * 100
            rs_ok = diff >= p.rs_min_pct
            rs_text = f"ecart avec le BTC sur {p.rs_bars} bougies : {diff:+.1f} %"
    check("force", rs_ok, 10, rs_text)

    # Risque (5)
    run = (close / c.close[i - p.late_bars] - 1) * 100
    check("risque", run < p.late_pct, 5,
          f"depart encore recent ({run:+.1f} % sur {p.late_bars} bougies)")

    if breakout and ev.score >= p.min_score and atr > 0:
        lo = max(0, i - p.stop_lookback + 1)
        swing = min(c.low[lo:i + 1])
        dist = close - (swing - p.stop_atr_buffer * atr)
        dist = min(max(dist, p.stop_min_atr * atr), p.stop_max_atr * atr)
        ev.stop = close - dist
        ev.tp1 = close + p.tp1_r * dist
        ev.tp2 = close + p.tp2_r * dist
        ev.is_signal = True
    return ev
