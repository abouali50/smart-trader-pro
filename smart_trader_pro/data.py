"""Donnees de marche publiques Binance Spot (aucune cle API requise).

Le miroir public data-api.binance.vision est essaye en premier : il reste
accessible depuis les serveurs heberges aux Etats-Unis, ou api.binance.com
est refuse.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import requests

DEFAULT_BASE_URLS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
)

INTERVAL_MS = {
    "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}

_EXCLUDED_BASES = {
    "USDC", "FDUSD", "TUSD", "DAI", "USDP", "BUSD", "EUR", "EURI", "AEUR",
    "USD1", "XUSD", "BFUSD", "USDE", "PAXG", "WBTC", "WBETH", "BNSOL",
}
_EXCLUDED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


@dataclass
class Candles:
    """Bougies cloturees, de la plus ancienne a la plus recente."""
    symbol: str
    interval: str
    ts: List[int] = field(default_factory=list)      # ouverture, en ms
    open: List[float] = field(default_factory=list)
    high: List[float] = field(default_factory=list)
    low: List[float] = field(default_factory=list)
    close: List[float] = field(default_factory=list)
    volume: List[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.ts)

    def append(self, ts: int, o: float, h: float, l: float, c: float, v: float) -> None:
        self.ts.append(ts)
        self.open.append(o)
        self.high.append(h)
        self.low.append(l)
        self.close.append(c)
        self.volume.append(v)


class MarketData:
    def __init__(self, base_urls: Sequence[str] = DEFAULT_BASE_URLS, timeout: int = 15):
        self.base_urls = list(base_urls)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "smart-trader-pro/1.0"

    def _get(self, path: str, params: Optional[dict] = None):
        last_error: Optional[Exception] = None
        for attempt in range(3):
            for base in self.base_urls:
                try:
                    r = self.session.get(base + path, params=params, timeout=self.timeout)
                    if r.status_code == 200:
                        return r.json()
                    last_error = RuntimeError(f"{base}{path} -> HTTP {r.status_code}: {r.text[:120]}")
                    if r.status_code in (418, 429):
                        time.sleep(5 * (attempt + 1))
                except requests.RequestException as exc:
                    last_error = exc
            time.sleep(1 + attempt)
        raise RuntimeError(f"Donnees de marche indisponibles : {last_error}")

    def klines(self, symbol: str, interval: str, total: int = 1000,
               now_ms: Optional[int] = None) -> Candles:
        """Renvoie les `total` dernieres bougies CLOTUREES."""
        if interval not in INTERVAL_MS:
            raise ValueError(f"Intervalle non gere : {interval}")
        now_ms = now_ms or int(time.time() * 1000)
        rows: list = []
        end: Optional[int] = None
        while len(rows) < total + 1:
            params = {"symbol": symbol, "interval": interval,
                      "limit": min(1000, total + 1 - len(rows))}
            if end is not None:
                params["endTime"] = end
            batch = self._get("/api/v3/klines", params)
            if not batch:
                break
            rows = batch + rows
            end = batch[0][0] - 1
            if len(batch) < params["limit"]:
                break
        out = Candles(symbol, interval)
        seen = set()
        for k in rows:
            if k[6] >= now_ms or k[0] in seen:   # bougie en cours ou doublon
                continue
            seen.add(k[0])
            out.append(int(k[0]), float(k[1]), float(k[2]), float(k[3]),
                       float(k[4]), float(k[5]))
        if len(out) > total:
            for name in ("ts", "open", "high", "low", "close", "volume"):
                setattr(out, name, getattr(out, name)[-total:])
        return out

    def top_symbols(self, quote: str = "USDT", count: int = 20,
                    min_quote_volume: float = 20_000_000) -> List[str]:
        """Paires les plus liquides sur 24 h, hors stablecoins et jetons a levier."""
        tickers = self._get("/api/v3/ticker/24hr")
        picked = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith(quote):
                continue
            base = sym[: -len(quote)]
            if base in _EXCLUDED_BASES or base.endswith(_EXCLUDED_SUFFIXES):
                continue
            qv = float(t.get("quoteVolume", 0))
            if qv >= min_quote_volume:
                picked.append((qv, sym))
        picked.sort(reverse=True)
        return [s for _, s in picked[:count]]
