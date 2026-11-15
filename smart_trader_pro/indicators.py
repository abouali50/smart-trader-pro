"""Indicateurs techniques en Python pur (aucune dependance).

Chaque fonction renvoie une liste de meme longueur que l'entree ; les
positions sans assez d'historique valent None. Aucune valeur ne depend
de donnees futures.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from typing import List, Optional, Sequence

Series = List[Optional[float]]


def sma(values: Sequence[float], period: int) -> Series:
    out: Series = [None] * len(values)
    if period <= 0:
        raise ValueError("period doit etre > 0")
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """Moyenne mobile exponentielle, amorcee par une SMA."""
    out: Series = [None] * len(values)
    if period <= 0:
        raise ValueError("period doit etre > 0")
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """RSI de Wilder."""
    out: Series = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain /= period
    loss /= period
    out[period] = _rsi_value(gain, loss)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gain = (gain * (period - 1) + max(d, 0.0)) / period
        loss = (loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_value(gain, loss)
    return out


def _rsi_value(gain: float, loss: float) -> float:
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gain / loss)


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14) -> Series:
    """Average True Range de Wilder."""
    n = len(closes)
    out: Series = [None] * n
    if n <= period:
        return out
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    prev = sum(trs[1:period + 1]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def macd_hist(closes: Sequence[float], fast: int = 12, slow: int = 26,
              signal: int = 9) -> Series:
    """Histogramme MACD (MACD - ligne de signal)."""
    n = len(closes)
    out: Series = [None] * n
    ef, es = ema(closes, fast), ema(closes, slow)
    start = slow - 1
    if n <= start:
        return out
    line = [ef[i] - es[i] for i in range(start, n)]  # type: ignore[operator]
    sig = ema(line, signal)
    for j, s in enumerate(sig):
        if s is not None:
            out[start + j] = line[j] - s
    return out


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0):
    """Renvoie (bande basse, bande mediane, bande haute)."""
    n = len(closes)
    mid = sma(closes, period)
    low: Series = [None] * n
    up: Series = [None] * n
    for i in range(period - 1, n):
        w = closes[i - period + 1:i + 1]
        m = mid[i]
        sd = (sum((x - m) ** 2 for x in w) / period) ** 0.5  # type: ignore[operator]
        low[i], up[i] = m - mult * sd, m + mult * sd          # type: ignore[operator]
    return low, mid, up


def obv(closes: Sequence[float], volumes: Sequence[float]) -> List[float]:
    """On-Balance Volume : volume cumule, signe par le sens de la cloture."""
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - volumes[i]
        else:
            out[i] = out[i - 1]
    return out


def mfi(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        volumes: Sequence[float], period: int = 14) -> Series:
    """Money Flow Index : un RSI pondere par le volume."""
    n = len(closes)
    out: Series = [None] * n
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    pos = [0.0] * n
    neg = [0.0] * n
    for i in range(1, n):
        flow = tp[i] * volumes[i]
        if tp[i] > tp[i - 1]:
            pos[i] = flow
        elif tp[i] < tp[i - 1]:
            neg[i] = flow
    p = sum(pos[1:period + 1])
    q = sum(neg[1:period + 1])
    for i in range(period, n):
        if i > period:
            p += pos[i] - pos[i - period]
            q += neg[i] - neg[i - period]
        if q <= 1e-12:
            out[i] = 100.0 if p > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + p / q)
    return out


def cmf(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        volumes: Sequence[float], period: int = 20) -> Series:
    """Chaikin Money Flow : > 0 = pression acheteuse, < 0 = vendeuse."""
    n = len(closes)
    out: Series = [None] * n
    mfv = []
    for i in range(n):
        rng = highs[i] - lows[i]
        mult = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / rng if rng > 0 else 0.0
        mfv.append(mult * volumes[i])
    a = b = 0.0
    for i in range(n):
        a += mfv[i]
        b += volumes[i]
        if i >= period:
            a -= mfv[i - period]
            b -= volumes[i - period]
        if i >= period - 1:
            out[i] = a / b if b > 0 else 0.0
    return out


def stoch_rsi(closes: Sequence[float], period: int = 14, smooth: int = 3) -> Series:
    """RSI stochastique lisse (ligne %K), entre 0 et 100."""
    n = len(closes)
    r = rsi(closes, period)
    raw: Series = [None] * n
    for i in range(n):
        if i - period + 1 < 0 or r[i - period + 1] is None:
            continue
        w = r[i - period + 1:i + 1]
        lo, hi = min(w), max(w)                     # type: ignore[type-var]
        raw[i] = 50.0 if hi == lo else (r[i] - lo) / (hi - lo) * 100  # type: ignore[operator]
    out: Series = [None] * n
    for i in range(n):
        if i - smooth + 1 >= 0 and raw[i - smooth + 1] is not None:
            out[i] = sum(raw[i - smooth + 1:i + 1]) / smooth  # type: ignore[arg-type]
    return out


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        period: int = 14):
    """Renvoie (ADX, +DI, -DI) de Wilder : force et sens de la tendance."""
    n = len(closes)
    out_adx: Series = [None] * n
    pdi: Series = [None] * n
    mdi: Series = [None] * n
    if n <= 2 * period:
        return out_adx, pdi, mdi
    tr = [0.0] * n
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm[i] = up if up > down and up > 0 else 0.0
        mdm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    s_tr = sum(tr[1:period + 1])
    s_p = sum(pdm[1:period + 1])
    s_m = sum(mdm[1:period + 1])
    dxs = []
    prev_adx = None
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + tr[i]
            s_p = s_p - s_p / period + pdm[i]
            s_m = s_m - s_m / period + mdm[i]
        p = 100 * s_p / s_tr if s_tr > 0 else 0.0
        m = 100 * s_m / s_tr if s_tr > 0 else 0.0
        pdi[i], mdi[i] = p, m
        dx = 100 * abs(p - m) / (p + m) if p + m > 0 else 0.0
        if prev_adx is None:
            dxs.append(dx)
            if len(dxs) == period:
                prev_adx = sum(dxs) / period
                out_adx[i] = prev_adx
        else:
            prev_adx = (prev_adx * (period - 1) + dx) / period
            out_adx[i] = prev_adx
    return out_adx, pdi, mdi


def fib_retracement(highs: Sequence[float], lows: Sequence[float], i: int,
                    window: int = 60):
    """Derniere impulsion haussiere vue a la bougie i, sans regarder apres i.

    Prend le plus haut des `window` dernieres bougies, puis le plus bas qui le
    precede dans la meme fenetre. Renvoie None si le plus haut est au tout
    debut de la fenetre (pas d'impulsion mesurable), sinon un dictionnaire :
    low, high, et les niveaux 0.382 / 0.5 / 0.618 / 0.786 du repli, plus
    l'extension 1.618.
    """
    start = max(0, i - window + 1)
    hi_idx = max(range(start, i + 1), key=lambda k: highs[k])
    if hi_idx == start:
        return None
    lo_idx = min(range(start, hi_idx), key=lambda k: lows[k])
    hi, lo = highs[hi_idx], lows[lo_idx]
    span = hi - lo
    if span <= 0:
        return None
    return {
        "low": lo, "high": hi, "high_idx": hi_idx, "low_idx": lo_idx,
        "0.382": hi - 0.382 * span, "0.5": hi - 0.5 * span,
        "0.618": hi - 0.618 * span, "0.786": hi - 0.786 * span,
        "1.618": lo + 1.618 * span,
    }
