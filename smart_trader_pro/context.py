"""Contexte de marche : CoinGecko et CoinMarketCap.

Ces sources ne declenchent pas de signal ; elles servent de garde-fou
(marche global en forte baisse) et d'information (dominance du BTC,
indice Peur et Avidite, cryptos en tendance).

- CoinGecko : fonctionne sans cle ; COINGECKO_API_KEY (cle demo gratuite)
  releve les limites de debit.
- CoinMarketCap : necessite CMC_API_KEY (offre gratuite sur
  pro.coinmarketcap.com). Sans cle, la source est simplement ignoree.

Toute panne d'une source est tolere : le scanner continue sans elle.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Set

import requests

COINGECKO = "https://api.coingecko.com/api/v3"
CMC = "https://pro-api.coinmarketcap.com"


@dataclass
class MarketContext:
    market_change_24h: Optional[float] = None    # % capitalisation totale
    btc_dominance: Optional[float] = None        # %
    fear_greed: Optional[int] = None             # 0 (peur) a 100 (avidite)
    fear_greed_label: str = ""
    trending: Set[str] = field(default_factory=set)
    sources: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def risk_off(self, drop_pct: float) -> bool:
        return (drop_pct > 0 and self.market_change_24h is not None
                and self.market_change_24h <= -drop_pct)

    def lines(self, symbol: str = "") -> List[str]:
        out = []
        if self.market_change_24h is not None:
            out.append(f"Marche global 24 h : {self.market_change_24h:+.2f} %")
        if self.btc_dominance is not None:
            out.append(f"Dominance BTC : {self.btc_dominance:.1f} %")
        if self.fear_greed is not None:
            out.append(f"Peur et Avidite : {self.fear_greed}/100 {self.fear_greed_label}".rstrip())
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        if base and base.upper() in self.trending:
            out.append(f"{base} est en tendance sur CoinGecko (attention a l'emballement)")
        return out


def _coingecko(ctx: MarketContext, session: requests.Session, timeout: int) -> None:
    headers = {}
    key = os.environ.get("COINGECKO_API_KEY", "")
    if key:
        headers["x-cg-demo-api-key"] = key
    r = session.get(f"{COINGECKO}/global", headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json().get("data", {})
    change = data.get("market_cap_change_percentage_24h_usd")
    if change is not None:
        ctx.market_change_24h = float(change)
    dom = data.get("market_cap_percentage", {}).get("btc")
    if dom is not None:
        ctx.btc_dominance = float(dom)
    r = session.get(f"{COINGECKO}/search/trending", headers=headers, timeout=timeout)
    r.raise_for_status()
    for coin in r.json().get("coins", []):
        sym = coin.get("item", {}).get("symbol")
        if sym:
            ctx.trending.add(str(sym).upper())
    ctx.sources.append("CoinGecko")


def _coinmarketcap(ctx: MarketContext, session: requests.Session, timeout: int) -> None:
    key = os.environ.get("CMC_API_KEY", "")
    if not key:
        return
    headers = {"X-CMC_PRO_API_KEY": key, "Accept": "application/json"}
    r = session.get(f"{CMC}/v1/global-metrics/quotes/latest", headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json().get("data", {})
    if data.get("btc_dominance") is not None:
        ctx.btc_dominance = float(data["btc_dominance"])
    usd = data.get("quote", {}).get("USD", {})
    change = usd.get("total_market_cap_yesterday_percentage_change")
    if change is not None and ctx.market_change_24h is None:
        ctx.market_change_24h = float(change)
    ctx.sources.append("CoinMarketCap")
    r = session.get(f"{CMC}/v3/fear-and-greed/latest", headers=headers, timeout=timeout)
    r.raise_for_status()
    fg = r.json().get("data", {})
    if fg.get("value") is not None:
        ctx.fear_greed = int(fg["value"])
        ctx.fear_greed_label = f"({fg.get('value_classification', '')})".replace("()", "")


class ContextProvider:
    """Recupere le contexte et le garde en cache pour menager les quotas."""

    def __init__(self, ttl_seconds: int = 900, timeout: int = 15):
        self.ttl = ttl_seconds
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "smart-trader-pro/1.0"
        self._cached: Optional[MarketContext] = None
        self._at = 0.0

    def get(self) -> MarketContext:
        if self._cached is not None and time.time() - self._at < self.ttl:
            return self._cached
        ctx = MarketContext()
        for name, fn in (("CoinGecko", _coingecko), ("CoinMarketCap", _coinmarketcap)):
            try:
                fn(ctx, self.session, self.timeout)
            except Exception as exc:              # une source en panne ne bloque rien
                ctx.errors.append(f"{name} : {exc}")
        self._cached, self._at = ctx, time.time()
        return ctx
