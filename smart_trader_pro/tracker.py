"""Suivi des alertes : ce que le prix a fait apres chaque signal.

Pour chaque alerte enregistree, on rejoue le plan annonce sur les bougies
qui ont suivi, avec les memes regles que le backtest :

  - entree a l'ouverture de la bougie qui suit le signal ;
  - sortie au stop, a l'objectif 2, ou a la cloture apres `MAX_BARS` bougies ;
  - si une bougie touche le stop et l'objectif, le stop l'emporte ;
  - frais deduits a l'entree et a la sortie.

Le resultat est exprime en % et en R (multiple du risque pris). C'est ce
bilan, accumule semaine apres semaine, qui dit si les alertes valent
quelque chose.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from .data import Candles, INTERVAL_MS

MAX_BARS = 72            # duree maximale d'un trade suivi, en bougies
FEE_PCT = 0.1            # frais par ordre, en %
RECHECK_BARS = 16        # une alerte encore ouverte est revue toutes les 16 bougies
MAX_FAILURES = 6         # apres 6 echecs de lecture, l'alerte est classee "inconnu"
CLOSED = ("tp2", "stop", "expired")


def settle(sig: dict, c: Candles, max_bars: int = MAX_BARS,
           fee_pct: float = FEE_PCT) -> Optional[dict]:
    """Resultat d'une alerte d'apres les bougies `c`. None s'il n'y a pas encore de bougie
    apres le signal."""
    step = INTERVAL_MS[c.interval]
    start = next((i for i, t in enumerate(c.ts) if t > sig["ts"]), None)
    if start is None:
        return None
    if c.ts[start] > sig["ts"] + step:
        return {"status": "unknown", "why": "bougies manquantes apres le signal"}
    entry, stop, tp1, tp2 = c.open[start], sig["stop"], sig["tp1"], sig["tp2"]
    if not (0 < stop < entry < tp2):
        return {"status": "invalid", "entry": entry,
                "why": "le cours avait deja depasse le stop ou l'objectif a l'entree"}
    risk = entry - stop
    status, exit_price, hit_tp1 = "open", c.close[-1], False
    high, low, bars = entry, entry, 0
    for i in range(start, min(start + max_bars, len(c))):
        bars += 1
        high, low = max(high, c.high[i]), min(low, c.low[i])
        if c.low[i] <= stop:
            status, exit_price = "stop", stop
            break
        if c.high[i] >= tp1:
            hit_tp1 = True
        if c.high[i] >= tp2:
            status, exit_price = "tp2", tp2
            break
        exit_price = c.close[i]
    else:
        if bars >= max_bars:
            status = "expired"
    fees = fee_pct / 100 * (entry + exit_price) if status in CLOSED else 0.0
    return {
        "status": status, "entry": entry, "exit": exit_price, "tp1": hit_tp1, "bars": bars,
        "pct": ((exit_price - entry) - fees) / entry * 100,
        "r": ((exit_price - entry) - fees) / risk,
        "max_pct": (high / entry - 1) * 100, "min_pct": (low / entry - 1) * 100,
    }


def update(signals: List[dict], fetch: Callable[[dict, str, int], Candles], now_ms: int,
           interval_of: Callable[[dict], str], limit: int = 120) -> Tuple[int, int]:
    """Met a jour le champ "outcome" des alertes encore ouvertes (les plus anciennes d'abord).

    `fetch(sig, interval, total)` renvoie les `total` dernieres bougies cloturees.
    Renvoie (alertes relues, alertes cloturees pendant ce passage).
    """
    checked = closed = 0
    for sig in sorted(signals, key=lambda s: s.get("ts", 0)):
        if checked >= limit:
            break
        out = sig.get("outcome") or {}
        if out.get("status", "open") != "open" or sig.get("next_check", 0) > now_ms:
            continue
        if not all(isinstance(sig.get(k), (int, float)) for k in ("ts", "stop", "tp1", "tp2")):
            sig["outcome"] = {"status": "unknown", "why": "alerte incomplete"}
            continue
        interval = interval_of(sig)
        step = INTERVAL_MS[interval]
        elapsed = (now_ms - sig["ts"]) // step - 1          # bougies cloturees depuis le signal
        if elapsed < 1:
            continue
        checked += 1
        try:
            c = fetch(sig, interval, int(min(1000, elapsed + 3)))
            res = settle(sig, c)
        except Exception as exc:
            sig["failures"] = sig.get("failures", 0) + 1
            sig["next_check"] = now_ms + step
            if sig["failures"] >= MAX_FAILURES:
                sig["outcome"] = {"status": "unknown", "why": f"lecture impossible : {exc}"[:120]}
            continue
        if res is None:
            sig["next_check"] = now_ms + step
            continue
        sig["outcome"] = res
        sig.pop("failures", None)
        if res["status"] == "open":
            expiry = sig["ts"] + (MAX_BARS + 2) * step
            sig["next_check"] = max(now_ms + step, min(now_ms + RECHECK_BARS * step, expiry))
        else:
            sig.pop("next_check", None)
            closed += res["status"] in CLOSED
    return checked, closed


def summary(signals: List[dict]) -> Dict[str, dict]:
    """Bilan par type d'alerte, sur les alertes cloturees."""
    groups: Dict[str, dict] = {}
    for sig in signals:
        g = groups.setdefault(sig.get("kind") or "repli", {
            "closed": 0, "open": 0, "wins": 0, "tp2": 0, "stop": 0, "expired": 0,
            "total_r": 0.0, "total_pct": 0.0})
        out = sig.get("outcome") or {}
        status = out.get("status", "open")
        if status == "open":
            g["open"] += 1
        elif status in CLOSED:
            g["closed"] += 1
            g[status] += 1
            g["wins"] += out["r"] > 0
            g["total_r"] += out["r"]
            g["total_pct"] += out["pct"]
    for g in groups.values():
        n = g["closed"]
        g["win_rate"] = g["wins"] / n * 100 if n else None
        g["avg_r"] = g["total_r"] / n if n else None
        g["avg_pct"] = g.pop("total_pct") / n if n else None
    return groups
