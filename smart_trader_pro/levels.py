"""Niveaux et flux d'ordres : VWAP, VWAP ancre, profil de volume, delta.

Ces mesures situent le prix par rapport a l'endroit ou le volume s'est
reellement echange, et disent qui, des acheteurs ou des vendeurs, paie
le prix du marche pour passer tout de suite.

- VWAP : prix moyen pondere par le volume sur les dernieres bougies.
- VWAP ancre : le meme calcul, demarre au dernier creux important. Il
  donne le prix de revient moyen de ceux qui ont achete depuis ce creux.
- Profil de volume : repartition du volume par niveau de prix. Le POC
  est le prix le plus echange ; la zone de valeur contient 70 % du volume.
- Flux d'ordres : delta = volume achete au marche - volume vendu au
  marche, par bougie et en cumule (CVD).

Le profil est estime a partir des bougies (le volume de chaque bougie est
reparti sur son etendue de prix), pas a partir de chaque transaction.
Tout est calcule sans regarder apres la bougie i.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from typing import List, Optional

from .data import Candles

VWAP_WINDOW = 24
ANCHOR_LOOKBACK = 96
PROFILE_WINDOW = 120
PROFILE_BINS = 24
VALUE_AREA = 0.70
FLOW_WINDOW = 24


def _fmt(x: float) -> str:
    if x >= 1000:
        return f"{x:,.2f}".replace(",", " ")
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}".rstrip("0")


def vwap(c: Candles, i: int, start: int) -> Optional[float]:
    """Prix moyen pondere par le volume des bougies start..i."""
    start = max(0, start)
    num = den = 0.0
    for k in range(start, i + 1):
        num += (c.high[k] + c.low[k] + c.close[k]) / 3 * c.volume[k]
        den += c.volume[k]
    return num / den if den > 0 else None


def anchor_index(c: Candles, i: int, lookback: int = ANCHOR_LOOKBACK) -> int:
    """Bougie du plus bas des `lookback` dernieres bougies : le point d'ancrage."""
    start = max(0, i - lookback + 1)
    return min(range(start, i + 1), key=lambda k: c.low[k])


def volume_profile(c: Candles, i: int, window: int = PROFILE_WINDOW,
                   bins: int = PROFILE_BINS) -> Optional[dict]:
    start = max(0, i - window + 1)
    lo, hi = min(c.low[start:i + 1]), max(c.high[start:i + 1])
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    hist = [0.0] * bins
    for k in range(start, i + 1):
        a = min(bins - 1, int((c.low[k] - lo) / step))
        b = min(bins - 1, int((c.high[k] - lo) / step))
        share = c.volume[k] / (b - a + 1)
        for j in range(a, b + 1):
            hist[j] += share
    total = sum(hist)
    if total <= 0:
        return None
    poc = max(range(bins), key=lambda j: hist[j])
    left = right = poc
    inside = hist[poc]
    while inside < VALUE_AREA * total and (left > 0 or right < bins - 1):
        below = hist[left - 1] if left > 0 else -1.0
        above = hist[right + 1] if right < bins - 1 else -1.0
        if above >= below:
            right += 1
            inside += hist[right]
        else:
            left -= 1
            inside += hist[left]
    return {"poc": lo + (poc + 0.5) * step, "val": lo + left * step,
            "vah": lo + (right + 1) * step}


def order_flow(c: Candles, i: int, window: int = FLOW_WINDOW) -> Optional[dict]:
    """Delta de la bougie et delta cumule, en part du volume (-1 a +1)."""
    start = max(0, i - window + 1)
    vol = sum(c.volume[start:i + 1])
    buy = sum(c.taker_buy[start:i + 1])
    if vol <= 0 or buy <= 0 or c.volume[i] <= 0:
        return None                         # volume acheteur non fourni
    bar_buy = c.taker_buy[i] / c.volume[i]
    return {"bar_buy": bar_buy, "bar_delta": 2 * bar_buy - 1,
            "cvd": (2 * buy - vol) / vol, "window": i - start + 1}


def analyze(c: Candles, i: int, book: Optional[float] = None) -> dict:
    """Toutes les mesures a la bougie i, avec leur lecture en clair.

    `book` : part des achats dans le carnet d'ordres a +/- 1 % du prix (0 a 1),
    mesuree en direct ; None si elle n'est pas disponible.
    """
    price = c.close[i]
    out: dict = {"lines": []}
    lines: List[str] = out["lines"]

    def gap(level: float) -> str:
        d = (price / level - 1) * 100
        return f"prix {'au-dessus' if d >= 0 else 'en dessous'} ({d:+.1f} %)"

    v = vwap(c, i, i - VWAP_WINDOW + 1)
    if v:
        out["vwap"] = v
        lines.append(f"VWAP {min(VWAP_WINDOW, i + 1)} bougies : {_fmt(v)} - {gap(v)}")
    a = anchor_index(c, i)
    av = vwap(c, i, a)
    if av:
        out["avwap"], out["anchor_ts"] = av, c.ts[a]
        lines.append(f"VWAP ancre au dernier creux (il y a {i - a} bougies) : {_fmt(av)} - {gap(av)}")
    prof = volume_profile(c, i)
    if prof:
        out.update(prof)
        where = ("au-dessus de la zone de valeur" if price > prof["vah"]
                 else "sous la zone de valeur" if price < prof["val"]
                 else "dans la zone de valeur")
        lines.append(f"Profil de volume : POC {_fmt(prof['poc'])}, zone de valeur "
                     f"{_fmt(prof['val'])} - {_fmt(prof['vah'])} ; prix {where}")
    flow = order_flow(c, i)
    if flow:
        out["flow"] = flow
        lines.append(f"Flux d'ordres, derniere bougie : {flow['bar_buy'] * 100:.0f} % d'achats "
                     f"au marche (delta {flow['bar_delta'] * 100:+.0f} % du volume)")
        side = ("acheteurs dominants" if flow["cvd"] > 0.03
                else "vendeurs dominants" if flow["cvd"] < -0.03 else "equilibre")
        lines.append(f"Delta cumule sur {flow['window']} bougies : "
                     f"{flow['cvd'] * 100:+.0f} % du volume ({side})")
    else:
        lines.append("Flux d'ordres : donnee indisponible pour cette paire")
    if book is not None:
        out["book"] = book
        lines.append(f"Carnet d'ordres a +/- 1 % du prix : {book * 100:.0f} % d'achats en attente")
    return out
