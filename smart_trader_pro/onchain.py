"""Flux on-chain d'un jeton : qui achete et qui vend sur les DEX.

Pour un jeton connu par son contrat, on lit ses principaux pools de
liquidite (API on-chain de CoinGecko) : nombre de portefeuilles
acheteurs et vendeurs, volume echange, liquidite. C'est une information
que les bougies ne contiennent pas : une cassure portee par beaucoup
d'acheteurs differents n'a pas le meme sens qu'une cassure faite par
deux ou trois gros ordres.

Chaque appel coute un credit de l'abonnement CoinGecko : le nombre de
jetons interroges par passage est plafonne, et les reponses sont
gardees en memoire un quart d'heure.

Ce n'est pas une prediction. Le verdict est enregistre avec chaque
alerte pour que le bilan montre, avec le temps, s'il aide vraiment.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

import requests

PRO_API = "https://pro-api.coingecko.com/api/v3/onchain"
NETWORKS = {"56": "bsc", "1": "eth", "8453": "base", "42161": "arbitrum", "501": "solana", "CT_501": "solana"}
CACHE_SECONDS = 14 * 60
MIN_WALLETS = 10          # en dessous, la part d'acheteurs ne veut rien dire
THIN_LIQUIDITY = 100_000  # liquidite jugee faible, en dollars


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.1f} M $"
    if x >= 1e3:
        return f"{x / 1e3:.0f} k $"
    return f"{x:.0f} $"


def summarize(pools: List[dict]) -> Optional[dict]:
    """Agrege les trois pools les plus liquides d'un jeton. None s'il n'y a rien d'exploitable."""
    rows = []
    for p in pools or []:
        a = p.get("attributes") or {}
        if _num(a.get("reserve_in_usd")) > 0:
            rows.append(a)
    rows.sort(key=lambda a: _num(a.get("reserve_in_usd")), reverse=True)
    rows = rows[:3]
    if not rows:
        return None

    def tx(window: str, field: str) -> float:
        return sum(_num(((a.get("transactions") or {}).get(window) or {}).get(field)) for a in rows)

    def vol(window: str) -> float:
        return sum(_num((a.get("volume_usd") or {}).get(window)) for a in rows)

    liquidity = sum(_num(a.get("reserve_in_usd")) for a in rows)
    buyers, sellers = tx("h1", "buyers"), tx("h1", "sellers")
    unit = "portefeuilles"
    if buyers + sellers == 0:                       # certains pools ne donnent que le nombre d'ordres
        buyers, sellers, unit = tx("h1", "buys"), tx("h1", "sells"), "ordres"
    total = buyers + sellers
    share = buyers / total if total >= MIN_WALLETS else None
    v15, v1h, v24 = vol("m15"), vol("h1"), vol("h24")
    accel = v15 * 4 / v1h if v1h > 0 and v15 > 0 else None

    lines = []
    if share is None:
        lines.append(f"Trop peu d'activite sur les DEX depuis 1 h ({int(total)} {unit}) pour conclure")
    else:
        lines.append(f"Acheteurs : {share * 100:.0f} % des {unit} actifs sur 1 h "
                     f"({int(buyers)} acheteurs / {int(sellers)} vendeurs)")
    if accel is not None:
        lines.append(f"Volume DEX sur 15 min : {_money(v15)}, rythme x{accel:.1f} par rapport a l'heure")
    if v24 > 0:
        lines.append(f"Volume DEX sur 24 h : {_money(v24)}")
    lines.append(f"Liquidite des pools : {_money(liquidity)}"
                 + (" - faible, le prix peut decrocher vite" if liquidity < THIN_LIQUIDITY else ""))

    if share is None:
        verdict = "neutre"
    elif share <= 0.45:
        verdict = "contredit"
    elif share >= 0.55 and (accel is None or accel >= 1.0):
        verdict = "confirme"
    else:
        verdict = "neutre"
    label = {"confirme": "Les acheteurs dominent on-chain : le mouvement est confirme",
             "contredit": "Les vendeurs dominent on-chain : mefiance, la hausse n'est pas suivie",
             "neutre": "Pas de signal net on-chain"}[verdict]
    return {"verdict": verdict, "label": label, "lines": lines, "buyer_share": share,
            "buyers": int(buyers), "sellers": int(sellers), "volume_15m": v15, "volume_1h": v1h,
            "volume_24h": v24, "acceleration": accel, "liquidity": liquidity, "pools": len(rows)}


class OnChain:
    def __init__(self, api_key: Optional[str] = None, per_pass: int = 8, session=None, timeout: int = 12):
        self.api_key = api_key if api_key is not None else os.environ.get("COINGECKO_PRO_API_KEY", "")
        self.per_pass = per_pass
        self.session = session or requests.Session()
        self.timeout = timeout
        self.cache: Dict[str, tuple] = {}
        self.used = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and self.per_pass > 0

    def start_pass(self) -> None:
        """A appeler au debut de chaque passage : remet le compteur d'appels a zero."""
        self.used = 0
        now = time.time()
        self.cache = {k: v for k, v in self.cache.items() if v[0] > now - CACHE_SECONDS}

    def flow(self, chain_id, contract: str) -> Optional[dict]:
        """Flux on-chain du jeton, ou None (reseau non gere, plafond atteint, panne)."""
        network = NETWORKS.get(str(chain_id))
        if not self.enabled or not network or not contract:
            return None
        key = f"{network}:{contract.lower()}"
        hit = self.cache.get(key)
        if hit and hit[0] > time.time() - CACHE_SECONDS:
            return hit[1]
        if self.used >= self.per_pass:
            return None
        self.used += 1
        try:
            r = self.session.get(f"{PRO_API}/networks/{network}/tokens/{contract}/pools",
                                 headers={"x-cg-pro-api-key": self.api_key}, timeout=self.timeout)
            if r.status_code != 200:
                self.failed += 1
                return None
            result = summarize((r.json() or {}).get("data"))
        except (requests.RequestException, ValueError):
            self.failed += 1
            return None
        self.cache[key] = (time.time(), result)
        return result
