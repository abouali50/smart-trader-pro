"""Confirmation sur d'autres plateformes : Coinbase, OKX et Gate.io.

Quand le radar voit un depart sur Binance, on verifie si le volume
s'emballe aussi ailleurs. Un mouvement visible sur plusieurs plateformes
est plus credible qu'un pic isole sur une seule.

Donnees publiques, sans cle. Une plateforme qui ne repond pas, ou qui ne
cote pas la crypto, est simplement ignoree.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Tuple

import requests

HOUR = 3600
BASELINE = 20          # heures servant de reference
CONFIRM_RATIO = 2.0    # volume x2 = confirmation


def _ratio(candles: List[Tuple[int, float]], now: float) -> Optional[float]:
    """candles : (debut en secondes, volume). Volume de la derniere heure close / moyenne."""
    closed = sorted((t, v) for t, v in candles if t + HOUR <= now)
    if len(closed) < BASELINE // 2 + 1:
        return None
    last = closed[-1][1]
    ref = [v for _, v in closed[-(BASELINE + 1):-1]]
    mean = sum(ref) / len(ref)
    return last / mean if mean > 0 else None


def _coinbase(base: str, s: requests.Session, timeout: int) -> List[Tuple[int, float]]:
    r = s.get(f"https://api.exchange.coinbase.com/products/{base}-USD/candles",
              params={"granularity": HOUR}, timeout=timeout)
    r.raise_for_status()
    return [(int(k[0]), float(k[5])) for k in r.json()[:40]]


def _okx(base: str, s: requests.Session, timeout: int) -> List[Tuple[int, float]]:
    r = s.get("https://www.okx.com/api/v5/market/candles",
              params={"instId": f"{base}-USDT", "bar": "1H", "limit": 40}, timeout=timeout)
    r.raise_for_status()
    return [(int(k[0]) // 1000, float(k[5])) for k in r.json().get("data", [])]


def _gate(base: str, s: requests.Session, timeout: int) -> List[Tuple[int, float]]:
    r = s.get("https://api.gateio.ws/api/v4/spot/candlesticks",
              params={"currency_pair": f"{base}_USDT", "interval": "1h", "limit": 40},
              timeout=timeout)
    r.raise_for_status()
    return [(int(k[0]), float(k[6])) for k in r.json()]


SOURCES: Dict[str, Callable] = {"Coinbase": _coinbase, "OKX": _okx, "Gate.io": _gate}


class Exchanges:
    def __init__(self, timeout: int = 8):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "smart-trader-pro/1.1"

    def confirmations(self, symbol: str, now: Optional[float] = None) -> List[dict]:
        """Pour chaque plateforme : {"name", "ratio" (ou None), "confirmed"}."""
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        now = now or time.time()
        out = []
        for name, fetch in SOURCES.items():
            ratio = None
            try:
                ratio = _ratio(fetch(base, self.session, self.timeout), now)
            except Exception:                 # non cotee, panne, format inattendu
                ratio = None
            out.append({"name": name, "ratio": ratio,
                        "confirmed": ratio is not None and ratio >= CONFIRM_RATIO})
        return out


def summary_lines(conf: List[dict]) -> List[str]:
    """Texte pour une alerte."""
    known = [c for c in conf if c["ratio"] is not None]
    if not known:
        return ["Autres plateformes : aucune donnee (crypto non cotee ou service indisponible)"]
    ok = sum(c["confirmed"] for c in known)
    lines = [f"Autres plateformes : volume confirme sur {ok} sur {len(known)}"]
    lines += [f"  {c['name']} : volume x{c['ratio']:.1f}" for c in known]
    return lines
