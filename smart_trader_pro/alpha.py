"""Binance Alpha : les jetons de la rubrique Alpha de Binance.

Binance Alpha regroupe des jetons jeunes, negocies avant une eventuelle
cotation classique. Leurs mouvements sont violents dans les deux sens.
Ce module lit la liste des jetons et leurs bougies via l'API publique de
Binance Alpha (aucune cle requise).

Ces adresses sont servies par www.binance.com : elles peuvent etre
refusees depuis certains pays. Dans ce cas le module se desactive seul.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import requests

from .data import Candles, INTERVAL_MS

BASE = "https://www.binance.com"
TOKEN_LIST = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
KLINES = "/bapi/defi/v1/public/alpha-trade/klines"


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


class AlphaData:
    def __init__(self, timeout: int = 15, pause: float = 0.05):
        self.timeout = timeout
        self.pause = pause
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; smart-trader-pro/1.5)",
            "Accept": "application/json",
        })
        self.tokens_by_id: Dict[str, dict] = {}

    def _get(self, path: str, params: Optional[dict] = None):
        r = self.session.get(BASE + path, params=params, timeout=self.timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Binance Alpha -> HTTP {r.status_code}")
        body = r.json()
        if not isinstance(body, dict) or not body.get("success", False):
            raise RuntimeError(f"Binance Alpha -> reponse refusee ({str(body)[:80]})")
        return body.get("data") or []

    def tokens(self, min_volume: float = 200_000, min_liquidity: float = 50_000,
               count: int = 120) -> List[dict]:
        """Jetons Alpha negociables, du plus echange au moins echange."""
        picked = []
        for t in self._get(TOKEN_LIST):
            if not isinstance(t, dict) or not t.get("alphaId") or not t.get("symbol"):
                continue
            if t.get("offline") or t.get("stockState") or t.get("offsell"):
                continue                       # retire, action tokenisee, vente seule
            vol, liq = _num(t.get("volume24h")), _num(t.get("liquidity"))
            if vol < min_volume or liq < min_liquidity:
                continue
            picked.append({
                "id": str(t["alphaId"]),
                "symbol": str(t["symbol"]).upper(),
                "name": str(t.get("name") or t["symbol"]),
                "chain": str(t.get("chainName") or ""),
                "price": _num(t.get("price")),
                "change_24h": _num(t.get("percentChange24h")),
                "volume": vol, "liquidity": liq,
                "mcap": _num(t.get("marketCap")),
                "holders": int(_num(t.get("holders"))),
                "listed_spot": bool(t.get("listingCex")),
            })
        picked.sort(key=lambda t: t["volume"], reverse=True)
        picked = picked[:count]
        self.tokens_by_id = {t["id"]: t for t in picked}
        return picked

    def klines(self, alpha_id: str, interval: str, total: int = 200,
               now_ms: Optional[int] = None) -> Candles:
        """Bougies CLOTUREES du jeton contre l'USDT."""
        if interval not in INTERVAL_MS:
            raise ValueError(f"Intervalle non gere : {interval}")
        now_ms = now_ms or int(time.time() * 1000)
        rows = self._get(KLINES, {"symbol": f"{alpha_id}USDT", "interval": interval,
                                  "limit": min(1500, total + 1)})
        name = self.tokens_by_id.get(alpha_id, {}).get("symbol", alpha_id)
        out = Candles(f"{name}USDT", interval)
        seen = set()
        for k in rows:
            if not isinstance(k, (list, tuple)) or len(k) < 7:
                continue
            ts = int(k[0])
            if int(k[6]) >= now_ms or ts in seen:      # bougie en cours ou doublon
                continue
            seen.add(ts)
            out.append(ts, _num(k[1]), _num(k[2]), _num(k[3]), _num(k[4]), _num(k[5]),
                       _num(k[9]) if len(k) > 9 else 0.0)
        order = sorted(range(len(out)), key=lambda i: out.ts[i])
        for field in ("ts", "open", "high", "low", "close", "volume", "taker_buy"):
            values = getattr(out, field)
            setattr(out, field, [values[i] for i in order][-total:])
        if self.pause:
            time.sleep(self.pause)
        return out


def info_lines(t: dict) -> List[str]:
    """Carte d'identite du jeton, pour une alerte ou le detail d'une ligne."""
    def money(x: float) -> str:
        for unit, div in (("Md", 1e9), ("M", 1e6), ("k", 1e3)):
            if x >= div:
                return f"{x / div:.1f} {unit} $"
        return f"{x:.0f} $"
    lines = [f"{t['name']} sur {t['chain'] or 'chaine inconnue'}",
             f"Capitalisation {money(t['mcap'])} - liquidite {money(t['liquidity'])} - "
             f"volume 24 h {money(t['volume'])}",
             f"{t['holders']} detenteurs"]
    if t["listed_spot"]:
        lines.append("Deja cote sur Binance Spot")
    return lines
