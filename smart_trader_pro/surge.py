"""Alerte rapide : un depart en cours, sans attendre la fin de la bougie.

Le signal d'explosion attend la cloture d'une bougie de 15 minutes, puis
le passage sur toutes les paires : l'alerte peut arriver un quart d'heure
apres le debut du mouvement. Cette voie rapide regarde tout le marche
une fois par minute, en un seul appel, et compare avec la photo prise
quelques minutes plus tot :

  - le prix a pris au moins `surge_min_move` % en quelques minutes ;
  - le volume echange sur ces minutes est au moins `surge_vol_mult` fois
    le rythme moyen des dernieres 24 heures ;
  - le prix est passe au-dessus du plus haut des 24 dernieres heures.

Le prix d'entree affiche est le prix au moment de l'alerte, pas celui
d'une bougie deja terminee.

Contrepartie : une alerte donnee plus tot, sur un mouvement qui n'est
pas confirme par une cloture, donne plus de faux departs. Ces alertes
sont suivies a part dans le bilan, qui dira si la vitesse paie.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from . import indicators as ind
from .data import Candles, INTERVAL_MS

Snapshot = Dict[str, Tuple[float, float]]
TP1_R, TP2_R = 1.5, 3.0


def detect(old: Snapshot, new: Snapshot, minutes: float, min_move: float, vol_mult: float,
           min_volume: float) -> List[dict]:
    """Paires dont le prix et le volume s'emballent entre deux photos du marche."""
    out = []
    if minutes <= 0:
        return out
    for sym, (price, qvol) in new.items():
        before = old.get(sym)
        if not before or before[0] <= 0 or qvol < min_volume:
            continue
        move = (price / before[0] - 1) * 100
        pace = (qvol - before[1]) / minutes            # volume par minute depuis l'ancienne photo
        usual = qvol / 1440                            # rythme moyen sur 24 h
        ratio = pace / usual if usual > 0 else 0.0
        if move >= min_move and ratio >= vol_mult:
            out.append({"symbol": sym, "price": price, "move": move, "ratio": ratio,
                        "minutes": minutes, "volume_24h": qvol})
    out.sort(key=lambda c: c["ratio"], reverse=True)
    return out


def plan(cand: dict, c: Candles, lookback: int = 96, atr_mult: float = 1.5,
         min_risk_pct: float = 1.5, max_run_pct: float = 12.0) -> Optional[dict]:
    """Verifie la cassure sur les bougies cloturees et construit le plan. None si le depart
    n'en est pas un (pas de cassure, ou mouvement deja trop avance)."""
    if len(c) < lookback + 15:
        return None
    high = max(c.high[-lookback:])
    price = cand["price"]
    if price <= high:
        return None                                   # pas encore au-dessus du plus haut de 24 h
    run = (price / c.close[-lookback] - 1) * 100
    if run > max_run_pct:
        return None                                   # deja trop monte : on n'envoie pas en retard
    atr = ind.atr(c.high, c.low, c.close, 14)[-1]
    if not atr:
        return None
    dist = max(atr_mult * atr, price * min_risk_pct / 100)
    return {**cand, "broken": high, "run": run, "stop": price - dist, "tp1": price + TP1_R * dist,
            "tp2": price + TP2_R * dist, "risk_pct": dist / price * 100,
            "ts": c.ts[-1] + INTERVAL_MS[c.interval]}   # bougie en cours : le suivi entre a la suivante


def format_alert(p: dict, fmt_price) -> str:
    base = p["symbol"]
    return "\n".join([
        f"⚡ DEPART EN COURS - {base}",
        f"Prix maintenant : {fmt_price(p['price'])} ({p['move']:+.1f} % en {p['minutes']:.0f} min)",
        f"Volume : x{p['ratio']:.0f} par rapport au rythme des dernieres 24 h",
        f"Plus haut de 24 h casse : {fmt_price(p['broken'])} - hausse sur 24 h : {p['run']:+.1f} %",
        "",
        f"Entree : {fmt_price(p['price'])} (prix au moment de l'alerte)",
        f"Stop : {fmt_price(p['stop'])} (-{p['risk_pct']:.2f} %)",
        f"Objectif 1 : {fmt_price(p['tp1'])}",
        f"Objectif 2 : {fmt_price(p['tp2'])}",
        "",
        "Alerte rapide, sur une bougie non terminee : elle arrive plus tot, donc avec plus de faux "
        "departs. Si le prix a deja depasse l'objectif 1 quand tu la lis, il est trop tard.",
        "Analyse automatique, pas un conseil. Aucun ordre n'est passe.",
    ])


class SurgeWatcher:
    def __init__(self, cfg: dict, md):
        self.cfg, self.md = cfg, md
        self.snaps: deque = deque(maxlen=8)            # (horodatage, photo)
        self.cooldown: Dict[str, float] = {}

    def tick(self, now: Optional[float] = None) -> List[dict]:
        """Une verification. Renvoie les plans des departs detectes (deja filtres)."""
        now = now or time.time()
        new = self.md.snapshot("USDT")
        window = float(self.cfg["surge_window_min"]) * 60
        old = next(((t, s) for t, s in self.snaps if now - t >= window * 0.75), None)
        self.snaps.append((now, new))
        while self.snaps and now - self.snaps[0][0] > window * 2:
            self.snaps.popleft()
        if old is None:
            return []
        cands = detect(old[1], new, (now - old[0]) / 60, float(self.cfg["surge_min_move"]),
                       float(self.cfg["surge_vol_mult"]), float(self.cfg["surge_min_volume"]))
        hold = float(self.cfg["surge_cooldown_hours"]) * 3600
        excluded = set(self.cfg.get("exclude") or [])
        plans = []
        for cand in cands[: int(self.cfg["surge_max_alerts"]) * 3]:
            sym = cand["symbol"]
            last = self.cooldown.get(sym)
            if sym in excluded or (last is not None and now - last < hold):
                continue
            try:
                c = self.md.klines(sym, self.cfg["radar_interval"], 130)
            except Exception:
                continue
            p = plan(cand, c)
            if p is None:
                continue
            self.cooldown[sym] = now
            plans.append(p)
            if len(plans) >= int(self.cfg["surge_max_alerts"]):
                break
        return plans
