"""Marche large : reperer les petites cryptos qui se reveillent.

Binance ne cote que quelques centaines de cryptos. Ce module balaie les
4000 premieres par capitalisation via CoinGecko et cherche celles dont le
volume et le prix s'emballent par rapport a leurs propres habitudes.

Limites a connaitre :
- CoinGecko donne un volume cumule sur 24 h, pas des bougies : un reveil
  est vu avec un leger retard, et aucun stop ne peut etre calcule ici ;
- les petites cryptos sont peu liquides et souvent manipulees ;
- la methode n'a pas pu etre testee sur le passe (pas d'historique). Le
  module tient donc le bilan de ses propres alertes, 24 h apres chacune.

Auteur : Aziz Mernissi
"""

from __future__ import annotations

import json
import os
import statistics
import time
from typing import Callable, Dict, List, Optional

import requests

API = "https://api.coingecko.com/api/v3"
PRO_API = "https://pro-api.coingecko.com/api/v3"
PER_PAGE = 250
HISTORY = 48            # echantillons gardes par crypto
MIN_SAMPLES = 4         # avant de comparer une crypto a ses habitudes
MAJOR = ("Binance", "Coinbase Exchange", "OKX", "Gate", "Bybit", "KuCoin", "MEXC",
         "Bitget", "Kraken", "HTX", "Upbit", "Crypto.com Exchange")

DEFAULTS = {
    "wide_pages": 16,                 # 16 x 250 = 4000 cryptos
    "wide_interval_min": 15,
    "wide_min_volume": 300_000,       # USD sur 24 h
    "wide_min_mcap": 1_000_000,
    "wide_min_score": 70,
    "wide_cooldown_hours": 24,
    "wide_state_file": "wide_state.json",
    "wide_file": "wide.json",
}


def is_noise(coin: dict) -> bool:
    """Stablecoins et jetons enveloppes : leur prix suit autre chose."""
    sym = str(coin.get("symbol", "")).lower()
    name = str(coin.get("name", "")).lower()
    price = coin.get("current_price") or 0
    week = abs(coin.get("price_change_percentage_7d_in_currency") or 0)
    if "usd" in sym or "usd" in name or "eur" in sym:
        return True
    if 0.97 < price < 1.03 and week < 1.5:
        return True
    return any(w in name for w in ("wrapped", "staked", "bridged", "restaked", "tokenized"))


def score(coin: dict, vol_x: Optional[float]) -> tuple:
    """Note de reveil sur 100 et raisons. `vol_x` : volume / habitude, ou None."""
    h1 = coin.get("price_change_percentage_1h_in_currency") or 0.0
    d1 = coin.get("price_change_percentage_24h_in_currency") or 0.0
    d7 = coin.get("price_change_percentage_7d_in_currency") or 0.0
    mcap = coin.get("market_cap") or 0
    turnover = (coin.get("total_volume") or 0) / mcap if mcap > 0 else 0.0
    pts, why = 0, []
    if vol_x is not None:
        got = 40 if vol_x >= 3 else 30 if vol_x >= 2 else 20 if vol_x >= 1.5 else 0
        pts += got
        if got:
            why.append(f"volume x{vol_x:.1f} par rapport a ses habitudes")
    got = 25 if h1 >= 10 else 20 if h1 >= 6 else 10 if h1 >= 3 else 0
    pts += got
    if got:
        why.append(f"prix {h1:+.1f} % en 1 h")
    got = 15 if turnover >= 1 else 10 if turnover >= 0.3 else 0
    pts += got
    if got:
        why.append(f"volume 24 h = {turnover * 100:.0f} % de la capitalisation")
    if d1 < 30:
        pts += 10
        why.append(f"hausse 24 h encore limitee ({d1:+.1f} %)")
    if d7 < 80:
        pts += 10
        why.append(f"pas encore envolee sur 7 j ({d7:+.1f} %)")
    return pts, why


class WideScanner:
    def __init__(self, cfg: dict, sleep: Callable[[float], None] = time.sleep,
                 timeout: int = 20):
        self.cfg = {**DEFAULTS, **{k: v for k, v in cfg.items() if k in DEFAULTS}}
        self.sleep = sleep
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "smart-trader-pro/1.2"
        # Sans cle : acces public, lent mais sans plafond mensuel. La cle "demo"
        # gratuite n'est pas utilisee ici : son plafond de 10 000 appels par mois
        # serait epuise en quelques jours par ce balayage. Une cle payante
        # (COINGECKO_PRO_API_KEY) rend le balayage rapide et fiable.
        pro = os.environ.get("COINGECKO_PRO_API_KEY", "")
        self.api = PRO_API if pro else API
        if pro:
            self.session.headers["x-cg-pro-api-key"] = pro
        self.pause = 0.4 if pro else 7.0
        self.errors: List[str] = []

    # ---------- acces a CoinGecko ----------
    def _get(self, path: str, params: dict):
        for attempt in range(3):
            r = self.session.get(self.api + path, params=params, timeout=self.timeout)
            if r.status_code == 429:               # quota depasse : on patiente
                self.sleep(60)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("quota CoinGecko depasse")

    def fetch_market(self) -> List[dict]:
        coins: List[dict] = []
        for page in range(1, int(self.cfg["wide_pages"]) + 1):
            try:
                batch = self._get("/coins/markets", {
                    "vs_currency": "usd", "order": "market_cap_desc", "per_page": PER_PAGE,
                    "page": page, "price_change_percentage": "1h,24h,7d"})
            except Exception as exc:
                self.errors.append(f"page {page} : {exc}")
                break
            if not batch:
                break
            coins += batch
            if page < self.cfg["wide_pages"]:
                self.sleep(self.pause)
        return coins

    def where_to_trade(self, coin_id: str) -> List[str]:
        try:
            data = self._get(f"/coins/{coin_id}/tickers", {"depth": "false"})
        except Exception:
            return []
        names = []
        for t in data.get("tickers", []):
            n = (t.get("market") or {}).get("name")
            if n and n not in names:
                names.append(n)
        known = [n for n in MAJOR if n in names]
        return (known + [n for n in names if n not in known])[:6]

    # ---------- etat ----------
    def _load(self) -> dict:
        try:
            with open(self.cfg["wide_state_file"], encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = {}
        state.setdefault("hist", {})
        state.setdefault("alerts", [])
        state.setdefault("cooldown", {})
        return state

    def _save(self, path: str, data) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)

    # ---------- balayage ----------
    def sweep(self, now: Optional[float] = None) -> dict:
        """Un balayage complet. Renvoie le resultat (aussi ecrit dans wide_file)."""
        self.errors = []
        now = now or time.time()
        now_ms = int(now * 1000)
        cfg = self.cfg
        state = self._load()
        hist: Dict[str, list] = state["hist"]
        coins = self.fetch_market()
        prices = {c["id"]: c.get("current_price") for c in coins if c.get("id")}

        rows, learning = [], 0
        for c in coins:
            cid, mcap, vol = c.get("id"), c.get("market_cap") or 0, c.get("total_volume") or 0
            if not cid or not c.get("current_price") or mcap <= 0:
                continue
            ratio = vol / mcap
            past = hist.get(cid, [])
            vol_x = None
            if len(past) >= MIN_SAMPLES:
                base = statistics.median(past)
                vol_x = ratio / base if base > 0 else None
            hist[cid] = (past + [round(ratio, 6)])[-HISTORY:]
            if (vol < cfg["wide_min_volume"] or mcap < cfg["wide_min_mcap"] or is_noise(c)):
                continue
            if vol_x is None:
                learning += 1
            pts, why = score(c, vol_x)
            h1 = c.get("price_change_percentage_1h_in_currency") or 0.0
            alert = (pts >= cfg["wide_min_score"] and h1 >= 3
                     and vol_x is not None and vol_x >= 1.5)
            rows.append({
                "id": cid, "symbol": str(c.get("symbol", "")).upper(), "name": c.get("name", ""),
                "price": c["current_price"], "score": pts, "alert": alert, "reasons": why,
                "chg_1h": h1,
                "chg_24h": c.get("price_change_percentage_24h_in_currency"),
                "chg_7d": c.get("price_change_percentage_7d_in_currency"),
                "volume": vol, "mcap": mcap, "rank": c.get("market_cap_rank"), "vol_x": vol_x,
            })
        if coins:                                   # ne pas effacer l'historique sur une panne
            live = set(prices)
            state["hist"] = {k: v for k, v in hist.items() if k in live}

        # nouvelles alertes
        cooldown_ms = int(cfg["wide_cooldown_hours"] * 3_600_000)
        new_alerts = []
        for r in sorted(rows, key=lambda r: r["score"], reverse=True):
            if not r["alert"] or now_ms - state["cooldown"].get(r["id"], 0) < cooldown_ms:
                continue
            r["exchanges"] = self.where_to_trade(r["id"])
            self.sleep(self.pause)
            state["cooldown"][r["id"]] = now_ms
            entry = {k: r[k] for k in ("id", "symbol", "name", "price", "score", "chg_1h",
                                       "chg_24h", "vol_x", "mcap", "rank", "exchanges")}
            entry.update(ts=now_ms, last_price=r["price"], result_24h=None)
            new_alerts.append(entry)

        # bilan : que sont devenues les alertes precedentes ?
        for a in state["alerts"]:
            p = prices.get(a["id"])
            if p and a.get("result_24h") is None:
                a["last_price"] = p
                if now_ms - a["ts"] >= 86_400_000:
                    a["result_24h"] = (p / a["price"] - 1) * 100
        state["alerts"] = (new_alerts + state["alerts"])[:80]
        state["cooldown"] = {k: v for k, v in state["cooldown"].items()
                             if now_ms - v < 7 * 86_400_000}
        self._save(cfg["wide_state_file"], state)

        done = [a["result_24h"] for a in state["alerts"] if a.get("result_24h") is not None]
        rows.sort(key=lambda r: (r["alert"], r["score"]), reverse=True)
        result = {
            "updated": now_ms, "scanned": len(coins), "eligible": len(rows),
            "learning": learning, "min_score": cfg["wide_min_score"],
            "rows": rows[:15], "alerts": state["alerts"][:30], "new": new_alerts,
            "errors": self.errors,
            "summary": {
                "count": len(done),
                "average": sum(done) / len(done) if done else None,
                "positive_pct": 100 * sum(x > 0 for x in done) / len(done) if done else None,
            },
        }
        self._save(cfg["wide_file"], {k: v for k, v in result.items() if k != "new"})
        return result


def money(x: float) -> str:
    for unit, div in (("Md", 1e9), ("M", 1e6), ("k", 1e3)):
        if x >= div:
            return f"{x / div:.1f} {unit} $"
    return f"{x:.0f} $"


def format_alert(a: dict) -> str:
    lines = [
        f"REVEIL - {a['name']} ({a['symbol']})",
        f"Score : {a['score']}/100",
        "",
        f"Prix : {a['price']:.8g} $",
        f"Sur 1 h : {a['chg_1h']:+.1f} %" + (f"   Sur 24 h : {a['chg_24h']:+.1f} %"
                                           if a.get("chg_24h") is not None else ""),
        f"Volume : x{a['vol_x']:.1f} par rapport a ses habitudes",
        f"Capitalisation : {money(a['mcap'])}" + (f" (rang {a['rank']})" if a.get("rank") else ""),
        "Ou la trouver : " + (", ".join(a["exchanges"]) if a.get("exchanges") else "non precise"),
        f"https://www.coingecko.com/fr/pieces/{a['id']}",
        "",
        "Petite crypto : peu liquide, souvent manipulee, sortie parfois impossible.",
        "Aucun stop n'est calcule ici. Analyse automatique, pas un conseil.",
    ]
    return "\n".join(lines)
