"""Actualites crypto : flux RSS publics, rattaches aux cryptos qu'ils citent.

Les titres servent de contexte : ils apparaissent sur le tableau de bord
et dans les alertes de la crypto concernee. Ils ne declenchent pas de
signal a eux seuls.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional

import requests

FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
}
COINGECKO_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"
MAX_BYTES = 2_000_000

# Symboles ou noms qui sont aussi des mots courants : exiges avec le signe $.
AMBIGUOUS = {
    "LINK", "NEAR", "ONE", "GAS", "FLOW", "SUN", "WIN", "HOT", "KEY", "FUN", "FOR",
    "JUST", "TRUMP", "OM", "IO", "GO", "ACT", "ME", "MOVE", "NOT", "PEOPLE", "REZ",
    "S", "T", "W", "A", "D", "G", "U", "ID", "AI", "IP", "OP", "UP", "CAT", "DOGS",
    "MASK", "TURBO", "PUMP", "USUAL", "FORM", "SIGN", "INIT", "HOME", "LAYER",
}


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    ts: int                                   # ms
    coins: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"title": self.title, "link": self.link, "source": self.source,
                "ts": self.ts, "coins": self.coins}


def parse_rss(xml_text: str, source: str) -> List[NewsItem]:
    """Lit un flux RSS. Refuse les declarations d'entites (protection)."""
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link.startswith("https://"):
            continue
        try:
            ts = int(parsedate_to_datetime(it.findtext("pubDate") or "").timestamp() * 1000)
        except (TypeError, ValueError):
            continue
        items.append(NewsItem(re.sub(r"\s+", " ", title)[:220], link, source, ts))
    return items


def match_coins(title: str, names: Dict[str, str]) -> List[str]:
    """Cryptos citees dans un titre. `names` : symbole -> nom (ex. SOL -> Solana)."""
    found = []
    for sym, name in names.items():
        hit = re.search(r"\$" + re.escape(sym) + r"\b", title) is not None
        if not hit and sym not in AMBIGUOUS and len(sym) >= 3:
            hit = re.search(r"(?<![A-Za-z0-9$])" + re.escape(sym) + r"(?![A-Za-z0-9])", title) is not None
        if (not hit and name and len(name) >= 4 and name.upper() not in AMBIGUOUS
                and name.upper() != sym):
            hit = re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])",
                            title, re.IGNORECASE) is not None
        if hit:
            found.append(sym)
    return found


class NewsProvider:
    def __init__(self, ttl_seconds: int = 600, timeout: int = 12,
                 feeds: Optional[Dict[str, str]] = None):
        self.ttl = ttl_seconds
        self.timeout = timeout
        self.feeds = feeds or FEEDS
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "smart-trader-pro/1.1"
        self._items: List[NewsItem] = []
        self._at = 0.0
        self._names: Dict[str, str] = {}
        self._names_at = 0.0
        self.errors: List[str] = []

    def _coin_names(self, symbols: Iterable[str]) -> Dict[str, str]:
        """Noms des cryptos via CoinGecko, gardes un jour. Sans reponse : symboles seuls."""
        if not self._names or time.time() - self._names_at > 86_400:
            try:
                r = self.session.get(COINGECKO_MARKETS, timeout=self.timeout, params={
                    "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": 1})
                r.raise_for_status()
                names: Dict[str, str] = {}
                for coin in r.json():
                    names.setdefault(str(coin.get("symbol", "")).upper(), str(coin.get("name", "")))
                self._names, self._names_at = names, time.time()
            except Exception as exc:
                self.errors.append(f"noms des cryptos : {exc}")
        return {s: self._names.get(s, "") for s in symbols}

    def latest(self, symbols: Iterable[str], max_age_hours: int = 24) -> List[NewsItem]:
        """Titres recents, du plus neuf au plus ancien, avec les cryptos citees."""
        bases = sorted({s[:-4] if s.endswith("USDT") else s for s in symbols})
        if self._items and time.time() - self._at < self.ttl:
            return self._items
        self.errors = []
        collected: List[NewsItem] = []
        for source, url in self.feeds.items():
            try:
                r = self.session.get(url, timeout=self.timeout)
                r.raise_for_status()
                collected += parse_rss(r.text[:MAX_BYTES], source)
            except Exception as exc:
                self.errors.append(f"{source} : {exc}")
        names = self._coin_names(bases)
        cutoff = (time.time() - max_age_hours * 3600) * 1000
        seen, items = set(), []
        for it in sorted(collected, key=lambda x: x.ts, reverse=True):
            if it.ts < cutoff or it.ts > (time.time() + 3600) * 1000 or it.title in seen:
                continue
            seen.add(it.title)
            it.coins = match_coins(it.title, names)
            items.append(it)
        if items or not self._items:
            self._items, self._at = items, time.time()
        return self._items


def for_coin(items: List[NewsItem], symbol: str, limit: int = 2) -> List[NewsItem]:
    base = symbol[:-4] if symbol.endswith("USDT") else symbol
    return [it for it in items if base in it.coins][:limit]
